import os
import time
import json
import requests
import threading
import asyncio
import logging
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv

# ------------------ إعدادات وتهيئة ------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

load_dotenv()
logger.info("▶️ [START] loaded environment")

# ---- متغيرات البيئة ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID", "wf_691ac2e8aa388190a7b428f30a6ed0170545bfe7")
WORKFLOW_VERSION = os.getenv("WORKFLOW_VERSION", "1")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

if not OPENAI_API_KEY:
    logger.critical("❌ OPENAI_API_KEY غير موجود في .env")
    raise SystemExit(1)

# ---- اتصال MongoDB ----
try:
    client_db = MongoClient(MONGO_URI) if MONGO_URI else None
    db = client_db["multi_platform_bot"] if client_db else None
    sessions_collection = db["sessions"] if db else None
    if sessions_collection is None:
        logger.warning("⚠️ لم يتم إعداد MongoDB - sessions_collection فارغ. تأكد من MONGO_URI إذا كنت تريد حفظ جلسات.")
    else:
        logger.info("✅ [DB] MongoDB متصل.")
except Exception as e:
    logger.critical(f"❌ فشل الاتصال بقاعدة البيانات: {e}", exc_info=True)
    raise SystemExit(1)

# ---- إعداد OpenAI و Flask ----
client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)
logger.info("🚀 Flask و OpenAI client جاهزين.")

# ------------------ إعداد debounce/queue ------------------
pending_messages = {}   # user_id -> {"texts": [...], "session": session_doc}
message_timers = {}     # user_id -> threading.Timer
processing_locks = {}   # user_id -> threading.Lock
BATCH_WAIT_TIME = 2.0   # seconds

# ------------------ دوال الجلسات ------------------
def get_or_create_session_from_contact(contact_data):
    user_id = str(contact_data.get("id"))
    if not user_id:
        logger.error(f"❌ لم أجد user id في contact_data: {contact_data}")
        return None

    session = None
    if sessions_collection:
        session = sessions_collection.find_one({"_id": user_id})

    now_utc = datetime.now(timezone.utc)
    contact_source = (contact_data.get("source") or "").lower()
    if "instagram" in contact_source or contact_data.get("ig_id"):
        main_platform = "Instagram"
    else:
        main_platform = "Facebook"

    if session:
        update_fields = {
            "last_contact_date": now_utc,
            "platform": main_platform,
            "profile.name": contact_data.get("name"),
            "profile.profile_pic": contact_data.get("profile_pic"),
            "status": "active",
        }
        try:
            sessions_collection.update_one({"_id": user_id}, {"$set": {k: v for k, v in update_fields.items() if v is not None}})
            session = sessions_collection.find_one({"_id": user_id})
            logger.info(f"🔄 [SESSION] تم تحديث الجلسة للمستخدم {user_id}.")
        except Exception as e:
            logger.warning(f"⚠️ [DB] تعذر تحديث الجلسة: {e}")
    else:
        new_session = {
            "_id": user_id,
            "platform": main_platform,
            "profile": {
                "name": contact_data.get("name"),
                "first_name": contact_data.get("first_name"),
                "last_name": contact_data.get("last_name"),
                "profile_pic": contact_data.get("profile_pic"),
            },
            "status": "active",
            "first_contact_date": now_utc,
            "last_contact_date": now_utc,
        }
        if sessions_collection:
            try:
                sessions_collection.insert_one(new_session)
                session = sessions_collection.find_one({"_id": user_id})
                logger.info(f"🆕 [SESSION] تم إنشاء جلسة جديدة للمستخدم {user_id}.")
            except Exception as e:
                logger.error(f"❌ [DB] خطأ عند إنشاء جلسة جديدة: {e}", exc_info=True)
                return None
        else:
            # إذا Mongo غير معطّل، فقط أعد الـ dict المؤقت
            session = new_session
            logger.info(f"ℹ️ [SESSION] Mongo غير مفعل - إعادة جلسة مؤقتة للمستخدم {user_id}.")

    return session

# ------------------ دالة إرسال ManyChat ------------------
def send_manychat_reply_async(subscriber_id, text_message, platform):
    logger.info(f"📤 Sending to {subscriber_id} via {platform} ...")
    if not MANYCHAT_API_KEY:
        logger.error("❌ MANYCHAT_API_KEY غير موجود!")
        return

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform == "Instagram" else "facebook"

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message.strip()}] }},
        "channel": channel,
    }

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=20)
        resp.raise_for_status()
        logger.info(f"✅ ManyChat: message sent to {subscriber_id}.")
    except requests.exceptions.HTTPError as e:
        logger.error(f"❌ ManyChat HTTPError: {e} - {getattr(e.response, 'text', '')}")
    except Exception as e:
        logger.error(f"❌ ManyChat sending error: {e}", exc_info=True)

# ------------------ دالة استدعاء Responses API للـ Workflow ------------------
async def get_assistant_reply(session, content, timeout=90):
    user_id = session["_id"]
    logger.info(f"🤖 Requesting workflow reply for {user_id} ...")

    # نجهز المدخلات: هنا نرسل الرسالة كمحتوى واحد (role user)
    inputs = [{"role": "user", "content": content}]

    try:
        response = await asyncio.to_thread(
            client.responses.create,
            model=f"workflow:{WORKFLOW_ID}",
            input=inputs,
            version=WORKFLOW_VERSION,
            # يمكنك إضافة: max_output_tokens=..., temperature=... إذا رغبت
        )
        reply = getattr(response, "output_text", None)
        if reply:
            reply = reply.strip()
            logger.info(f"🗣️ Assistant replied (preview): {reply[:200]}")
            return reply
        # fallback: محاولة استخراج مكونات النص من output
        outputs = getattr(response, "output", None)
        if outputs and isinstance(outputs, list):
            accumulated = []
            for item in outputs:
                if isinstance(item, dict):
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            accumulated.append(c.get("text", ""))
            if accumulated:
                return "\n".join(accumulated).strip()
        logger.error("❌ No textual reply found in response.")
        return "⚠️ عفوًا، لم نستطع الحصول على رد واضح الآن."
    except Exception as e:
        logger.error(f"❌ Error calling Responses API: {e}", exc_info=True)
        return "⚠️ عفوًا، حدث خطأ أثناء توليد الرد."

# ------------------ معالجة الـ debounce وتجمّع الرسائل ------------------
def schedule_assistant_response(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        user_data = pending_messages.get(user_id)
        if not user_data:
            return
        session = user_data["session"]
        combined_content = "\n".join(user_data["texts"])
        logger.info(f"⚙️ Processing combined content for {user_id}: '{combined_content}'")
        try:
            reply_text = asyncio.run(get_assistant_reply(session, combined_content))
        except Exception as e:
            logger.error(f"❌ Exception while getting reply: {e}", exc_info=True)
            reply_text = "⚠️ عفوًا، تعذّر الحصول على رد الآن."

        if reply_text:
            send_manychat_reply_async(user_id, reply_text, platform=session.get("platform", "Facebook"))

        # تنظيف
        pending_messages.pop(user_id, None)
        timer = message_timers.pop(user_id, None)
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass
        logger.info(f"🗑️ Finished processing for {user_id}.")

def add_to_processing_queue(session, text_content):
    user_id = session["_id"]
    # حدّث last_contact_date في Mongo
    try:
        if sessions_collection:
            sessions_collection.update_one({"_id": user_id}, {"$set": {"last_contact_date": datetime.now(timezone.utc)}})
    except Exception as e:
        logger.warning(f"⚠️ Failed to update last_contact_date: {e}")

    # أضف للنطاق المؤقت
    if user_id in message_timers:
        try:
            message_timers[user_id].cancel()
            logger.info(f"⏳ Cancelled old timer for {user_id}.")
        except Exception:
            pass

    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session}
    pending_messages[user_id]["texts"].append(text_content)
    logger.info(f"➕ Queued message for {user_id}. queue_size={len(pending_messages[user_id]['texts'])}")

    timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[user_id])
    message_timers[user_id] = timer
    timer.start()
    logger.info(f"⏳ Started debounce timer ({BATCH_WAIT_TIME}s) for {user_id}.")

# ------------------ Webhook ManyChat ------------------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    logger.info("📞 Received webhook request.")
    auth_header = request.headers.get("Authorization")
    if not MANYCHAT_SECRET_KEY or auth_header != f"Bearer {MANYCHAT_SECRET_KEY}":
        logger.critical("🚨 Unauthorized webhook call!")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    data = request.get_json(silent=True)
    if not data or not data.get("full_contact"):
        logger.error("❌ Invalid webhook payload: missing full_contact")
        return jsonify({"status": "error", "message": "Invalid data"}), 400

    session = get_or_create_session_from_contact(data["full_contact"])
    if not session:
        return jsonify({"status": "error", "message": "Failed to create/get session"}), 500

    contact_data = data.get("full_contact", {})
    last_input = contact_data.get("last_text_input") or contact_data.get("last_input_text") or data.get("last_input")
    if not last_input:
        logger.warning("⚠️ No text input found in webhook.")
        return jsonify({"status": "no_input_received"})

    platform = session.get("platform", "Unknown")
    logger.info(f"🚦 Platform detected: {platform}")

    # أضف إلى قائمة المعالجة
    add_to_processing_queue(session, last_input)

    return jsonify({"status": "received"})

# ------------------ Homepage ------------------
@app.route("/")
def home():
    return "✅ Bot is running (Workflow Integration - Unified Mode)."

# ------------------ Run server (local) ------------------
if __name__ == "__main__":
    logger.info("🚀 Starting local server...")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
