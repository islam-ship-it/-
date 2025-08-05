import os
import time
import json
import requests
import threading
import asyncio
import logging
from flask import Flask, request, jsonify
from asgiref.wsgi import WsgiToAsgi
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from collections import deque  # ✅ تعديل: الرد على عميل واحد فقط كل 10 ثوانٍ

# ==============================================================================
# إعداد نظام التسجيل (Logging)
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

# ==============================================================================
# تحميل متغيرات البيئة
# ==============================================================================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# ==============================================================================
# إعدادات قاعدة البيانات (MongoDB)
# ==============================================================================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ تم الاتصال بقاعدة البيانات بنجاح.")
except Exception as e:
    logger.critical(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    exit()

# ==============================================================================
# إعداد تطبيق Flask وعميل OpenAI
# ==============================================================================
flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
client = OpenAI(api_key=OPENAI_API_KEY)

# ==============================================================================
# متغيرات عالمية
# ==============================================================================
pending_messages = {}
timers = {}
thread_locks = {}
client_processing_locks = {}
processing_queue = deque()  # ✅ قائمة انتظار جديدة
is_processing = False       # ✅ حالة لتفادي التداخل في المعالجة
# ==============================================================================
# دوال إدارة الجلسات (مشتركة)
# ==============================================================================
def get_session(user_id):
    user_id_str = str(user_id)
    session = sessions_collection.find_one({"_id": user_id_str})
    if not session:
        session = {
            "_id": user_id_str, "history": [], "thread_id": None,
            "message_count": 0, "name": "", "last_message_time": datetime.utcnow().isoformat(),
            "follow_up_sent": 0, "follow_up_status": "none", "last_follow_up_time": None,
            "payment_status": "pending"
        }
    return session

def save_session(user_id, session_data):
    user_id_str = str(user_id)
    session_data["_id"] = user_id_str
    sessions_collection.replace_one({"_id": user_id_str}, session_data, upsert=True)

# ==============================================================================
# دوال إرسال الرسائل
# ==============================================================================
def send_whatsapp_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        logger.info(f"📤 [WhatsApp] تم إرسال رسالة للعميل {phone}، الحالة: {response.status_code}")
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [WhatsApp] خطأ أثناء إرسال الرسالة عبر ZAPI: {e}")
# ==============================================================================
# منطق WhatsApp (Flask Webhook) - بالتعديل الجديد
# ==============================================================================
def process_whatsapp_messages(sender, name):
    sender_str = str(sender)
    with client_processing_locks.setdefault(sender_str, threading.Lock()):
        time.sleep(15)
        if not pending_messages.get(sender_str):
            timers.pop(sender_str, None)
            return

        combined_text = "\n".join(pending_messages[sender_str])
        reply = ask_assistant(combined_text, sender_str, name)

        time.sleep(3)  # ✅ تأخير ثابت لحماية الرقم

        send_whatsapp_message(sender_str, reply)

        time.sleep(1)  # ✅ تأخير إضافي بعد الإرسال

        pending_messages[sender_str] = []
        timers.pop(sender_str, None)

@flask_app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    sender = data.get("phone")
    if not sender: return jsonify({"status": "no sender"}), 400
    logger.info(f"📥 [WhatsApp] رسالة جديدة من: {sender}")
    session = get_session(sender)
    session["last_message_time"] = datetime.utcnow().isoformat()
    save_session(sender, session)
    name = data.get("pushname", "")
    msg = data.get("text", {}).get("message")
    image_url = data.get("image", {}).get("imageUrl")
    audio_url = data.get("audio", {}).get("audioUrl")

    if audio_url:
        transcribed_text = transcribe_audio(audio_url)
        if transcribed_text:
            reply = ask_assistant(f"رسالة صوتية من العميل: {transcribed_text}", sender, name)
            send_whatsapp_message(sender, reply)

    elif image_url:
        caption = data.get("image", {}).get("caption", "")
        content = [{"type": "image_url", "image_url": {"url": image_url}}]
        if caption: content.append({"type": "text", "text": f"تعليق على الصورة: {caption}"})
        reply = ask_assistant(content, sender, name)
        send_whatsapp_message(sender, reply)

    elif msg:
        sender_str = str(sender)
        if sender_str not in pending_messages:
            pending_messages[sender_str] = []
        pending_messages[sender_str].append(msg)

        # ✅ تعديل: بدل ما نشغّل Thread فورًا، نحط العميل في قائمة الانتظار
        if sender_str not in timers:
            processing_queue.append((sender_str, name))
            timers[sender_str] = True

    return jsonify({"status": "received"}), 200
# ==============================================================================
# منطق Telegram (Webhook)
# ==============================================================================
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

async def start_command(update, context):
    await update.message.reply_text(f"أهلاً {update.effective_user.first_name}!")

async def send_telegram_message(context, chat_id, message, business_connection_id=None):
    try:
        if business_connection_id:
            await context.bot.send_message(chat_id=chat_id, text=message, business_connection_id=business_connection_id)
        else:
            await context.bot.send_message(chat_id=chat_id, text=message)
        logger.info(f"📤 Sent to Telegram user {chat_id}")
    except Exception as e:
        logger.error(f"❌ Telegram send error: {e}")

async def handle_telegram_message(update, context):
    message = update.message or update.business_message
    if not message:
        return
    chat_id = message.chat.id
    user_name = message.from_user.first_name
    business_id = getattr(update.business_message, "business_connection_id", None) if hasattr(update, "business_message") else None

    logger.info("========== تحديث جديد تليجرام ==========")
    logger.info(f"🔍 chat_id: {chat_id}, name: {user_name}")
    logger.info(f"🔗 business_connection_id: {business_id}")
    logger.info("📦 full update:\n" + json.dumps(update.to_dict(), indent=2, ensure_ascii=False))

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING, business_connection_id=business_id)
    except Exception as e:
        logger.warning(f"⚠ لم يتمكن من إرسال chat action: {e}")

    session = get_session(chat_id)
    session["last_message_time"] = datetime.utcnow().isoformat()
    save_session(chat_id, session)

    reply_text = ""
    content_for_assistant = ""

    if message.text:
        content_for_assistant = message.text
    elif message.voice:
        voice_file = await message.voice.get_file()
        transcribed_text = transcribe_audio(voice_file.file_path)
        content_for_assistant = f"رسالة صوتية من العميل: {transcribed_text}" if transcribed_text else None
    elif message.photo:
        photo_file = await message.photo[-1].get_file()
        caption = message.caption or ""
        content_list = [{"type": "image_url", "image_url": {"url": photo_file.file_path}}]
        if caption: content_list.append({"type": "text", "text": f"تعليق على الصورة: {caption}"})
        content_for_assistant = content_list

    if content_for_assistant:
        reply_text = ask_assistant(content_for_assistant, chat_id, user_name)

    if reply_text:
        await send_telegram_message(context, chat_id, reply_text, business_connection_id=business_id)

telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(MessageHandler(filters.ALL, handle_telegram_message))

@flask_app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def telegram_webhook_handler():
    data = request.get_json()
    await telegram_app.process_update(telegram.Update.de_json(data, telegram_app.bot))
    return jsonify({"status": "ok"})

# ==============================================================================
# إعداد Webhook و تشغيل الجدولة
# ==============================================================================
@flask_app.route("/")
def home():
    return "✅ Bot is running"

async def setup_telegram():
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if host:
        await telegram_app.initialize()
        webhook_url = f"https://{host}/{TELEGRAM_BOT_TOKEN}"
        await telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=telegram.Update.ALL_TYPES)
        logger.info(f"✅ تم إعداد Telegram Webhook على: {webhook_url}")

try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(setup_telegram())
    else:
        asyncio.run(setup_telegram())
except Exception as e:
    logger.critical(f"❌ Telegram setup failed: {e}")

# ✅ جدولة الرد على عميل واحد كل 10 ثواني
def process_next_client():
    global is_processing
    if is_processing or not processing_queue:
        return
    is_processing = True
    sender_str, name = processing_queue.popleft()
    try:
        logger.info(f"🔄 جاري معالجة العميل: {sender_str}")
        process_whatsapp_messages(sender_str, name)
    finally:
        timers.pop(sender_str, None)
        is_processing = False

scheduler = BackgroundScheduler()
scheduler.add_job(process_next_client, 'interval', seconds=10)  # ✅ تنفيذ كل 10 ثواني
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=True)
