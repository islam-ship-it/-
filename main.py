import os
import time
import json
import requests
import threading
import asyncio
import logging
import base64
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv

# --- الإعدادات ---
# إعداد تسجيل مفصل مع طابع زمني لكل سجل
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)
load_dotenv()
logger.info("▶️ [START] تم تحميل إعدادات البيئة.")

# --- مفاتيح API ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN") # تم الاحتفاظ به للاستخدام المستقبلي
logger.info("🔑 [CONFIG] تم تحميل مفاتيح API.")

# --- قاعدة البيانات ---
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ [DB] تم الاتصال بقاعدة البيانات بنجاح.")
except Exception as e:
    logger.critical(f"❌ [DB] فشل الاتصال بقاعدة البيانات: {e}", exc_info=True)
    exit()

# --- إعدادات التطبيق ---
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("🚀 [APP] تم إعداد تطبيق Flask و OpenAI Client.")

# --- متغيرات عالمية للمعالجة غير المتزامنة ---
pending_messages = {}
message_timers = {}
processing_locks = {}
BATCH_WAIT_TIME = 2.0

# --- دوال إدارة الجلسات ---
def get_or_create_session_from_contact(contact_data):
    user_id = str(contact_data.get("id"))
    if not user_id:
        logger.error(f"❌ [SESSION] لم يتم العثور على user_id في البيانات: {contact_data}")
        return None
        
    session = sessions_collection.find_one({"_id": user_id})
    now_utc = datetime.now(timezone.utc)
    
    # تحديد المنصة بدقة
    main_platform = "Unknown"
    contact_source = contact_data.get("source", "").lower()
    if "instagram" in contact_source:
        main_platform = "Instagram"
    elif "facebook" in contact_source:
        main_platform = "Facebook"
    elif "ig_id" in contact_data and contact_data.get("ig_id"):
        main_platform = "Instagram"
    else:
        main_platform = "Facebook" # افتراضي

    logger.info(f"ℹ️ [SESSION] تم تحديد المنصة '{main_platform}' للمستخدم {user_id} من المصدر '{contact_source}'.")

    if session:
        update_fields = {
            "last_contact_date": now_utc, "platform": main_platform,
            "profile.name": contact_data.get("name"), "profile.profile_pic": contact_data.get("profile_pic"),
            "status": "active"
        }
        sessions_collection.update_one({"_id": user_id}, {"$set": {k: v for k, v in update_fields.items() if v is not None}})
        logger.info(f"🔄 [SESSION] تم تحديث الجلسة الحالية للمستخدم {user_id}.")
        return sessions_collection.find_one({"_id": user_id})
    else:
        logger.info(f"🆕 [SESSION] مستخدم جديد. جاري إنشاء جلسة شاملة له: {user_id} على منصة {main_platform}")
        new_session = {
            "_id": user_id, "platform": main_platform,
            "profile": {"name": contact_data.get("name"), "first_name": contact_data.get("first_name"), "last_name": contact_data.get("last_name"), "profile_pic": contact_data.get("profile_pic")},
            "openai_thread_id": None, "tags": [f"source:{main_platform.lower()}"],
            "custom_fields": contact_data.get("custom_fields", {}),
            "conversation_summary": "", "status": "active",
            "first_contact_date": now_utc, "last_contact_date": now_utc
        }
        sessions_collection.insert_one(new_session)
        return new_session

# --- دوال OpenAI ---
async def get_assistant_reply(session, content, timeout=90):
    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")
    logger.info(f"🤖 [ASSISTANT] بدء عملية الحصول على رد للمستخدم {user_id}.")
    if not thread_id:
        logger.warning(f"🧵 [ASSISTANT] لا يوجد thread للمستخدم {user_id}. سيتم إنشاء واحد جديد.")
        try:
            thread = await asyncio.to_thread(client.beta.threads.create)
            thread_id = thread.id
            sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
            logger.info(f"✅ [ASSISTANT] تم إنشاء وتخزين thread جديد: {thread_id}")
        except Exception as e:
            logger.error(f"❌ [ASSISTANT] فشل في إنشاء thread جديد: {e}", exc_info=True)
            return "⚠️ عفوًا، حدث خطأ أثناء تهيئة المحادثة."
    try:
        logger.info(f"💬 [ASSISTANT] إضافة رسالة إلى Thread {thread_id}: '{content}'")
        await asyncio.to_thread(client.beta.threads.messages.create, thread_id=thread_id, role="user", content=content)
        
        logger.info(f"▶️ [ASSISTANT] بدء تشغيل المساعد (Run) على Thread {thread_id}.")
        run = await asyncio.to_thread(client.beta.threads.runs.create, thread_id=thread_id, assistant_id=ASSISTANT_ID_PREMIUM)
        
        start_time = time.time()
        while run.status in ["queued", "in_progress"]:
            if time.time() - start_time > timeout:
                logger.error(f"⏰ [ASSISTANT] Timeout! استغرق الـ run {run.id} أكثر من {timeout} ثانية.")
                return "⚠️ حدث تأخير في الرد، يرجى المحاولة مرة أخرى."
            await asyncio.sleep(1)
            run = await asyncio.to_thread(client.beta.threads.runs.retrieve, thread_id=thread_id, run_id=run.id)
        
        if run.status == "completed":
            logger.info(f"✅ [ASSISTANT] اكتمل الـ Run بنجاح. جاري استرداد الرسائل...")
            messages = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=1)
            reply = messages.data[0].content[0].text.value.strip()
            logger.info(f"🗣️ [ASSISTANT] الرد الذي تم الحصول عليه: \"{reply}\"")
            return reply
        else:
            logger.error(f"❌ [ASSISTANT] لم يكتمل الـ run. الحالة: {run.status}. الخطأ: {run.last_error}")
            return "⚠️ عفوًا، حدث خطأ فني. فريقنا يعمل على إصلاحه."
    except Exception as e:
        logger.error(f"❌ [ASSISTANT] حدث استثناء غير متوقع: {e}", exc_info=True)
        return "⚠️ عفوًا، حدث خطأ غير متوقع."

# --- دوال المعالجة غير المتزامنة (للفيسبوك) ---
def send_manychat_reply_async(subscriber_id, text_message, platform):
    logger.info(f"📤 [MANYCHAT-ASYNC] بدء إرسال رد إلى {subscriber_id} على منصة {platform}...")
    if not MANYCHAT_API_KEY:
        logger.error("❌ [MANYCHAT-ASYNC] مفتاح MANYCHAT_API_KEY غير موجود!")
        return

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform == "Instagram" else "facebook"

    payload = {
        "subscriber_id": str(subscriber_id ),
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message.strip()}]}},
        "channel": channel,
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=20)
        response.raise_for_status()
        logger.info(f"✅ [MANYCHAT-ASYNC] تم إرسال الرسالة بنجاح إلى {subscriber_id} عبر {channel}.")
    except requests.exceptions.HTTPError as e:
        error_text = e.response.text if e.response is not None else str(e)
        logger.error(f"❌ [MANYCHAT-ASYNC] فشل إرسال الرسالة: {e}. تفاصيل الخطأ: {error_text}")
    except Exception as e:
        logger.error(f"❌ [MANYCHAT-ASYNC] خطأ غير متوقع أثناء الإرسال: {e}", exc_info=True)

def schedule_assistant_response(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages or not pending_messages[user_id]: return
        
        user_data = pending_messages[user_id]
        session = user_data["session"]
        combined_content = "\n".join(user_data["texts"])
        
        logger.info(f"⚙️ [BATCH] بدء معالجة المحتوى المجمع للمستخدم {user_id} على {session['platform']}: '{combined_content}'")
        reply_text = asyncio.run(get_assistant_reply(session, combined_content))
        
        if reply_text:
            send_manychat_reply_async(user_id, reply_text, platform=session["platform"])

        if user_id in pending_messages: del pending_messages[user_id]
        if user_id in message_timers: del message_timers[user_id]
        logger.info(f"🗑️ [BATCH] تم الانتهاء من المعالجة للمستخدم {user_id}.")

def add_to_processing_queue(session, text_content):
    user_id = session["_id"]
    if user_id in message_timers: message_timers[user_id].cancel()
    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session}
    pending_messages[user_id]["texts"].append(text_content)
    logger.info(f"➕ [QUEUE] تمت إضافة محتوى إلى قائمة الانتظار للمستخدم {user_id}. حجم القائمة الآن: {len(pending_messages[user_id]['texts'])}")
    timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

# --- ويب هوك ManyChat (النسخة الهجينة) ---
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    logger.info("📞 [WEBHOOK] تم استلام طلب جديد.")
    auth_header = request.headers.get('Authorization')
    if not MANYCHAT_SECRET_KEY or auth_header != f'Bearer {MANYCHAT_SECRET_KEY}':
        logger.critical("🚨 [WEBHOOK] محاولة وصول غير مصرح بها!")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    data = request.get_json()
    logger.info(f"📦 [WEBHOOK] البيانات المستلمة: {json.dumps(data, indent=2)}")

    if not data or not data.get("full_contact"):
        logger.error("❌ [WEBHOOK] CRITICAL: 'full_contact' غير موجودة في البيانات.")
        return jsonify({"status": "error", "message": "Invalid data"}), 400

    session = get_or_create_session_from_contact(data["full_contact"])
    if not session:
        logger.error("❌ [WEBHOOK] فشل في إنشاء أو الحصول على جلسة.")
        return jsonify({"status": "error", "message": "Failed to create session"}), 500

    # --- [التصحيح الرئيسي] استخلاص الإدخال الأخير بطريقة أكثر قوة ---
    contact_data = data.get("full_contact", {})
    last_input = contact_data.get("last_text_input") or \
                 contact_data.get("last_input_text") or \
                 data.get("last_input") # حقل احتياطي إضافي

    # --- تسجيلات إضافية للتشخيص ---
    logger.info(f"🔍 [DIAGNOSE] مفاتيح 'full_contact' المتاحة: {list(contact_data.keys())}")
    logger.info(f"📝 [DIAGNOSE] تم العثور على نص الرسالة: '{last_input}'")

    if not last_input:
        logger.warning("[WEBHOOK] لم يتم العثور على إدخال نصي للمعالجة. سيتم إرجاع رد فارغ.")
        return jsonify({"version": "v2", "content": {}})
    
    platform = session.get("platform", "Facebook")
    logger.info(f"🚦 [HYBRID] تم تحديد المنصة: {platform}. سيتم توجيه الطلب للمسار المناسب.")

    if platform == "Instagram":
        logger.info(f"⚡ [HYBRID] تفعيل المسار المتزامن (الفوري) لـ Instagram.")
        try:
            reply_text = asyncio.run(get_assistant_reply(session, last_input, timeout=25))
            
            if not reply_text:
                logger.warning("[HYBRID-SYNC] تم الحصول على رد فارغ من المساعد. سيتم إرسال رد فارغ.")
                return jsonify({"version": "v2", "content": {}})
            
            logger.info(f"✅ [HYBRID-SYNC] الرد جاهز للإرسال الفوري: \"{reply_text}\"")
            response_payload = {
                "version": "v2",
                "content": {"messages": [{"type": "text", "text": reply_text}]}
            }
            return jsonify(response_payload)
        except Exception as e:
            logger.error(f"❌ [HYBRID-SYNC] خطأ في المسار المتزامن: {e}", exc_info=True)
            error_response = {
                "version": "v2",
                "content": {"messages": [{"type": "text", "text": "عفوًا، حدث خطأ فني. يرجى المحاولة مرة أخرى."}]}
            }
            return jsonify(error_response)
    else:
        logger.info(f"🔄 [HYBRID] تفعيل المسار غير المتزامن (الخلفي) لـ {platform}.")
        add_to_processing_queue(session, last_input)
        logger.info("[HYBRID-ASYNC] تم إرسال الرسالة إلى قائمة الانتظار. سيتم إرجاع تأكيد استلام فوري.")
        return jsonify({"status": "received"})

# --- نقطة الدخول الرئيسية ---
@app.route("/")
def home():
    return "✅ Bot is running in Hybrid Mode (v17 - Enhanced Logging)."

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل. يرجى استخدام خادم WSGI (مثل Gunicorn) لتشغيله في بيئة الإنتاج.")
