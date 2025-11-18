import os
import time
import json
import requests
import threading
# تم إزالة 'asyncio' واستخدام العميل المتزامن
import openai
import logging
from flask import Flask, request, jsonify
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv

# --- الإعداد والتهيئة ---

# تهيئة نظام التسجيل (Logging)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__) 
logger.info("▶️ [START] Environment and Flask App Initializing...")

# تحميل المتغيرات البيئية
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

# التحقق من المتغيرات الضرورية
if not all([OPENAI_API_KEY, MONGO_URI, MANYCHAT_API_KEY, MANYCHAT_SECRET_KEY]):
    logger.critical("❌ [ENV] Missing one or more required environment variables (OPENAI_API_KEY, MONGO_URI, MANYCHAT_API_KEY, MANYCHAT_SECRET_KEY).")
    exit()

# --- إعداد قاعدة البيانات ---

try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ [DB] Connected to MongoDB successfully.")
except Exception as e:
    logger.critical(f"❌ [DB] Failed to connect: {e}", exc_info=True)
    exit()

# --- تطبيق Flask والحالة العامة ---

app = Flask(__name__)
# تهيئة عميل OpenAI الجديد
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# حالة تجميع الرسائل
pending_messages = {}
message_timers = {}
processing_locks = {}
BATCH_WAIT_TIME = 2.0 # وقت الانتظار لتجميع الرسائل

# --- وظائف مساعدة ---

def get_or_create_session(contact_data):
    """جلب أو إنشاء جلسة مستخدم في MongoDB."""
    user_id = str(contact_data.get("id"))
    if not user_id:
        logger.warning("⚠️ [DB] Received contact data without a valid ID.")
        return None

    session = sessions_collection.find_one({"_id": user_id})
    now = datetime.now(timezone.utc)

    # تحديد المنصة
    source = str(contact_data.get("source", "")).lower()
    platform = "Instagram" if "instagram" in source else "Facebook"

    if session:
        # تحديث الجلسة الموجودة
        sessions_collection.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact_date": now,
                "platform": platform,
                "profile.name": contact_data.get("name"),
                "profile.profile_pic": contact_data.get("profile_pic"),
            }}
        )
        return sessions_collection.find_one({"_id": user_id})

    # إنشاء جلسة جديدة
    new_session = {
        "_id": user_id,
        "platform": platform,
        "profile": {
            "name": contact_data.get("name"),
            "profile_pic": contact_data.get("profile_pic"),
        },
        "created": now,
        "last_contact_date": now,
    }

    sessions_collection.insert_one(new_session)
    logger.info(f"🆕 [SESSION] New session created for user: {user_id} on {platform}.")
    return new_session

def send_manychat_reply(subscriber_id, text, platform):
    """إرسال رسالة نصية ردًا عبر ManyChat."""
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    channel = "instagram" if platform == "Instagram" else "facebook"

    # ❗❗ التصحيح هنا: قص النص لضمان عدم تجاوز حد 2000 رمز ❗❗
    reply_text = text.strip()[:2000]

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {
            "version": "v2",
            "content": {
                "messages": [{"type": "text", "text": reply_text}]
            }
        },
        "channel": channel
    }

    logger.debug(f"🔍 [SEND] Payload for {subscriber_id}: {json.dumps(payload, indent=2)}")

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        logger.info(f"📤 [SEND] Message delivered to {subscriber_id} ({platform}).")
        logger.debug(f"📬 [RESPONSE] {r.text}")
    except requests.exceptions.HTTPError as err:
        logger.error(f"❌ [SEND] HTTPError for {subscriber_id}: {err}")
        logger.error(f"❌ [SEND] Response Text: {r.text}")
    except Exception as e:
        logger.error(f"❌ [SEND] Failed to send message to {subscriber_id}: {e}")

def run_agent_workflow(text, session):
    """استدعاء واجهة برمجة تطبيقات OpenAI Chat API لتوليد استجابة."""
    try:
        # تعليمات النظام لضبط شخصية البوت
        system_instruction = (
            "You are a helpful and friendly AI assistant integrated with a ManyChat flow. "
            "The user might send multiple messages quickly, which have been combined into the following prompt. "
            "Please respond concisely to all the user's combined messages. "
            f"The user's name is {session['profile']['name']} and they are on {session['platform']}."
        )

        response = openai_client.chat.completions.create(
            model="gpt-4o", # النموذج الموصى به
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": text},
            ],
            max_tokens=1000,
            temperature=0.7
        )
        
        # استخراج نص الاستجابة
        if response.choices and response.choices[0].message and response.choices[0].message.content:
            return response.choices[0].message.content.strip()
        
        logger.warning("⚠️ [AGENT] OpenAI response was empty or malformed.")
        return "⚠️ حدث خطأ أثناء معالجة طلبك: استجابة غير صالحة من AI."

    except openai.APIError as e:
        logger.error(f"❌ [AGENT] OpenAI API Error: {e}")
        return "⚠️ حدث خطأ في الاتصال بخدمة OpenAI. يرجى المحاولة لاحقًا."
    except Exception as e:
        logger.error(f"❌ [AGENT] Unknown Error: {e}")
        return "⚠️ حدث خطأ غير متوقع أثناء معالجة طلبك."

def schedule_message_processing(user_id):
    """الدالة التي يتم تنفيذها بواسطة المؤقت لمعالجة الرسائل المجمعة."""
    lock = processing_locks.setdefault(user_id, threading.Lock())
    # ضمان معالجة رسائل هذا المستخدم بواسطة خيط واحد فقط في كل مرة
    with lock:
        if user_id not in pending_messages:
            return

        data = pending_messages[user_id]
        session = data["session"]

        # دمج جميع الرسائل المستلمة في موجه واحد
        combined = "\n".join(data["texts"])
        logger.info(f"📦 [PROCESS] Processing batch for {user_id} on {session['platform']}. Combined text: '{combined[:100]}...'")

        # تشغيل سير عمل الوكيل المتزامن
        reply = run_agent_workflow(combined, session)

        # إرسال الرد النهائي
        send_manychat_reply(user_id, reply, session["platform"])

        # تنظيف الحالة المعلقة
        del pending_messages[user_id]
        if user_id in message_timers:
            del message_timers[user_id]
        logger.info(f"✅ [PROCESS] Batch completed and cleaned up for {user_id}.")


def add_to_queue(session, text):
    """إضافة رسالة جديدة إلى قائمة الانتظار وإعادة تعيين مؤقت التجميع."""
    user_id = session["_id"]

    # 1. إلغاء أي مؤقت يعمل حاليًا لهذا المستخدم
    if user_id in message_timers:
        message_timers[user_id].cancel()
        logger.debug(f"⏳ [QUEUE] Canceled existing timer for {user_id}.")

    # 2. إضافة الرسالة الجديدة إلى القائمة المعلقة
    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session}

    pending_messages[user_id]["texts"].append(text)
    logger.info(f"➕ [QUEUE] Added message for {user_id}. Current batch size: {len(pending_messages[user_id]['texts'])}")

    # 3. بدء مؤقت جديد
    timer = threading.Timer(BATCH_WAIT_TIME, schedule_message_processing, args=[user_id])
    message_timers[user_id] = timer
    timer.start()
    logger.debug(f"⏳ [QUEUE] New timer started for {user_id} set to {BATCH_WAIT_TIME}s.")

# --- مسارات Flask ---

@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    """معالجة طلبات ManyChat webhook الواردة."""
    auth = request.headers.get("Authorization")

    # فحص الأمان لمفتاح ManyChat السري
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        logger.error("❌ [WEBHOOK] Unauthorized access attempt.")
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact", {})

    logger.debug(f"Incoming Data: {json.dumps(data, indent=2)}")

    # جلب أو إنشاء جلسة المستخدم
    session = get_or_create_session(contact)
    if not session:
        logger.error("❌ [WEBHOOK] Failed to get/create session.")
        return jsonify({"error": "session-failed"}), 500

    # استخراج آخر نص إدخال للمستخدم
    last_input = (
        contact.get("last_text_input") or
        contact.get("last_input_text") or
        data.get("last_input")
    )

    if not last_input:
        return jsonify({"status": "no_input"})

    # إضافة الرسالة إلى قائمة الانتظار لمعالجتها على دفعات
    add_to_queue(session, last_input)

    return jsonify({"status": "received"})

@app.route("/")
def home():
    """مسار بسيط لفحص سلامة التطبيق."""
    return "🚀 Bot Running — Render Version"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
