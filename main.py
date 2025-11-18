import os
import logging
import openai
from flask import Flask, request, jsonify
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv
import re
import asyncio
import threading
import requests

logging.basicConfig(
    level=logging.DEBUG,  # استخدم DEBUG لعرض جميع التفاصيل
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
logger.info("▶️ [START] Environment Loaded.")

# تحميل إعدادات البيئة
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ [DB] Connected to MongoDB successfully.")
except Exception as e:
    logger.critical(f"❌ [DB] Failed to connect: {e}", exc_info=True)
    exit()

app = Flask(__name__)

# Set the OpenAI API key
openai.api_key = OPENAI_API_KEY

pending_messages = {}
message_timers = {}
processing_locks = {}
BATCH_WAIT_TIME = 2.0

def clean_text_for_messaging(text):
    """
    دالة لتنظيف النصوص من الرموز الغريبة أو غير الصالحة
    """
    cleaned_text = re.sub(r'[^\x00-\x7F\u0600-\u06FFa-zA-Z0-9\s]', '', text)  # يسمح فقط بالأحرف اللاتينية والعربية والأرقام
    cleaned_text = cleaned_text.strip()  # إزالة المسافات الزائدة
    logger.debug(f"Cleaned text: {cleaned_text}")
    return cleaned_text

def get_or_create_session(contact_data):
    user_id = str(contact_data.get("id"))
    if not user_id:
        logger.error("❌ [SESSION] User ID is missing.")
        return None

    session = sessions_collection.find_one({"_id": user_id})
    now = datetime.now(timezone.utc)

    # تحديد المنصة التي يتواصل منها المستخدم
    platform = "Instagram" if "instagram" in str(contact_data.get("source", "")).lower() else "Facebook"
    logger.debug(f"Detected platform: {platform}")

    if session:
        sessions_collection.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact_date": now,
                "platform": platform,  # تحديث المنصة إذا كانت مختلفة
                "profile.name": contact_data.get("name"),
                "profile.profile_pic": contact_data.get("profile_pic"),
            }}
        )
        logger.info(f"Session found and updated for user: {user_id}")
        return sessions_collection.find_one({"_id": user_id})

    # إنشاء جلسة جديدة للمستخدم بناءً على المنصة
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
    logger.info(f"New session created for user: {user_id}")
    return new_session

def send_manychat_reply(subscriber_id, text, platform):
    if not subscriber_id:
        logger.error("❌ [SEND] Subscriber ID is missing.")
        return

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    # تحديد القناة بناءً على المنصة
    channel = "instagram" if platform.lower() == "instagram" else "facebook"
    logger.debug(f"Sending message to channel: {channel}")

    # تنظيف النص قبل إرساله
    clean_text = clean_text_for_messaging(text)

    # تغليف النص في هيكلية JSON
    json_response = {
        "version": "v2",
        "content": {
            "messages": [
                {
                    "type": "text",
                    "text": clean_text
                }
            ]
        },
        "channel": channel
    }

    try:
        r = requests.post(url, json=json_response, headers=headers, timeout=20)
        r.raise_for_status()  # تحقق من أن الطلب تم بنجاح
        logger.info(f"📤 [SEND] Message delivered → {subscriber_id} on {channel}")
    except requests.exceptions.HTTPError as e:
        logger.error(f"❌ [SEND] Failed: {e.response.text}")  # سجل تفاصيل الخطأ
    except Exception as e:
        logger.error(f"❌ [SEND] Failed: {e}")

async def run_agent_workflow(text, session):
    try:
        # طباعة النص المرسل إلى الوكيل (OpenAI)
        logger.info(f"📤 [SEND TO AGENT] Text: {text}")

        # توليد النص عبر OpenAI API باستخدام الطريقة الحديثة chat.Completion.create مع نموذج GPT-4.1 Mini
        response = openai.chat.Completion.create(
            model="gpt-4.1-mini",  # تحديد النموذج GPT-4.1 Mini
            messages=[{"role": "user", "content": text}]  # إرسال النص كـ message
        )

        # طباعة النص الذي تم إرجاعه من الوكيل
        logger.info(f"📥 [RESPONSE FROM AGENT] Response: {response['choices'][0]['message']['content'].strip()}")

        return response['choices'][0]['message']['content'].strip()  # الحصول على النص الناتج من الرد
    except Exception as e:
        logger.error(f"❌ [AGENT] Error: {e}")
        return "⚠️ حدث خطأ أثناء معالجة طلبك."

def schedule_message_processing(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages:
            return

        data = pending_messages[user_id]
        session = data["session"]

        combined = "\n".join(data["texts"])
        logger.debug(f"🔍 [PROCESS] Combined text: {combined}")

        reply = asyncio.run(run_agent_workflow(combined, session))

        send_manychat_reply(user_id, reply, session["platform"])

        del pending_messages[user_id]
        if user_id in message_timers:
            del message_timers[user_id]

def add_to_queue(session, text):
    user_id = session["_id"]

    if user_id in message_timers:
        message_timers[user_id].cancel()

    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session}

    pending_messages[user_id]["texts"].append(text)

    timer = threading.Timer(BATCH_WAIT_TIME, schedule_message_processing, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    auth = request.headers.get("Authorization")

    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        logger.error("❌ [WEBHOOK] Unauthorized request.")
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact", {})

    session = get_or_create_session(contact)
    if not session:
        logger.error("❌ [SESSION] Failed to create or find session.")
        return jsonify({"error": "session-failed"}), 500

    last_input = (
        contact.get("last_text_input") or
        contact.get("last_input_text") or
        data.get("last_input")
    )

    if not last_input:
        logger.error("❌ [WEBHOOK] No user input received.")
        return jsonify({"status": "no_input"})

    logger.debug(f"Received input: {last_input}")
    add_to_queue(session, last_input)

    return jsonify({"status": "received"})

@app.route("/")
def home():
    return "🚀 Bot Running — Render Version"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
