import os
import json
import time
import logging
import requests
import threading
import asyncio
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from collections import deque
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram import Update
from asgiref.wsgi import WsgiToAsgi
from datetime import datetime

# ==================== إعدادات اللوج ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ==================== تحميل البيئة ====================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# ==================== إعداد التطبيقات ====================
flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
client = OpenAI(api_key=OPENAI_API_KEY)

# ==================== إعداد قاعدة البيانات ====================
client_db = MongoClient(MONGO_URI)
db = client_db["multi_platform_bot"]
sessions_collection = db["sessions"]

# ==================== المتغيرات العامة ====================
pending_messages = {}
timers = {}
processing_queue = deque()
client_locks = {}
is_processing = False

# ==================== الجلسات ====================
def get_session(user_id):
    uid = str(user_id)
    session = sessions_collection.find_one({"_id": uid}) or {
        "_id": uid, "history": [], "thread_id": None,
        "message_count": 0, "name": "", "last_message_time": datetime.utcnow().isoformat(),
        "follow_up_sent": 0, "follow_up_status": "none", "last_follow_up_time": None,
        "payment_status": "pending"
    }
    return session

def save_session(user_id, session):
    session["_id"] = str(user_id)
    sessions_collection.replace_one({"_id": session["_id"]}, session, upsert=True)

# ==================== إرسال رسالة واتساب ====================
def send_whatsapp_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        r = requests.post(url, headers=headers, json=payload)
        logger.info(f"📤 WhatsApp رسالة مرسلة إلى {phone} - {r.status_code}")
    except Exception as e:
        logger.error(f"❌ فشل إرسال واتساب: {e}")

# ==================== إرسال رسالة تليجرام ====================
async def send_telegram_message(context, chat_id, message, business_id=None):
    try:
        await context.bot.send_message(chat_id=chat_id, text=message, business_connection_id=business_id)
        logger.info(f"📤 Telegram رسالة مرسلة إلى {chat_id}")
    except Exception as e:
        logger.error(f"❌ فشل إرسال تليجرام: {e}")

# ==================== معالجة رسائل واتساب ====================
def process_whatsapp_messages(phone, name):
    time.sleep(10)
    combined = "
".join(pending_messages.get(phone, []))
    if not combined:
        return
    logger.info(f"🤖 إرسال للسيرفر: {combined}")
    reply = ask_assistant(combined, phone, name)
    send_whatsapp_message(phone, reply)
    pending_messages[phone] = []

@flask_app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    phone = data.get("phone")
    if not phone:
        return jsonify({"status": "no phone"}), 400

    name = data.get("pushname", "")
    msg = data.get("text", {}).get("message")
    image = data.get("image", {}).get("imageUrl")
    caption = data.get("image", {}).get("caption", "")

    session = get_session(phone)
    session["last_message_time"] = datetime.utcnow().isoformat()
    save_session(phone, session)

    if msg:
        pending_messages.setdefault(phone, []).append(msg)
        if phone not in timers:
            processing_queue.append((phone, name))
            timers[phone] = True
            logger.info(f"📥 رسالة واتساب من {phone}: {msg}")

    elif image:
        content = [{"type": "image_url", "image_url": {"url": image}}]
        if caption: content.append({"type": "text", "text": caption})
        reply = ask_assistant(content, phone, name)
        send_whatsapp_message(phone, reply)

    return jsonify({"status": "received"}), 200

# ==================== Telegram ====================
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

async def handle_telegram(update: Update, context):
    msg = update.message or update.business_message
    if not msg:
        return

    chat_id = msg.chat.id
    name = msg.from_user.first_name
    text = msg.text or ""
    business_id = getattr(update.business_message, "business_connection_id", None) if hasattr(update, "business_message") else None

    logger.info(f"📥 Telegram من {chat_id}: {text}")
    reply = ask_assistant(text, chat_id, name)
    await send_telegram_message(context, chat_id, reply, business_id)

telegram_app.add_handler(MessageHandler(filters.ALL, handle_telegram))

@flask_app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def telegram_webhook():
    data = request.get_json()
    await telegram_app.process_update(Update.de_json(data, telegram_app.bot))
    return jsonify({"status": "ok"})

# ==================== الرد من ChatGPT ====================
def ask_assistant(user_input, user_id, name=""):
    try:
        messages = [{"role": "user", "content": user_input}] if isinstance(user_input, str) else user_input
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7
        )
        reply = response.choices[0].message.content
        logger.info(f"🤖 رد ChatGPT: {reply}")
        return reply
    except Exception as e:
        logger.error(f"❌ خطأ في OpenAI: {e}")
        return "حصلت مشكلة فنية، حاول تاني بعد شوية."

# ==================== جدولة التنفيذ ====================
def process_next_client():
    global is_processing
    if is_processing or not processing_queue:
        return
    is_processing = True
    phone, name = processing_queue.popleft()
    try:
        logger.info(f"🔄 معالجة عميل: {phone}")
        process_whatsapp_messages(phone, name)
    finally:
        timers.pop(phone, None)
        is_processing = False

scheduler = BackgroundScheduler()
scheduler.add_job(process_next_client, 'interval', seconds=8)
scheduler.start()

@flask_app.route("/")
def index():
    return "✅ Bot is running"

async def setup_telegram():
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if host:
        await telegram_app.initialize()
        url = f"https://{host}/{TELEGRAM_BOT_TOKEN}"
        await telegram_app.bot.set_webhook(url=url)
        logger.info(f"✅ Webhook Telegram على: {url}")

try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(setup_telegram())
    else:
        asyncio.run(setup_telegram())
except Exception as e:
    logger.error(f"❌ Telegram Webhook Error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
