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
from collections import deque  # ✅ جديد

# إعداد اللوج
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# تحميل المتغيرات البيئية
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# الاتصال بقاعدة البيانات
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ تم الاتصال بقاعدة البيانات.")
except Exception as e:
    logger.critical(f"❌ قاعدة البيانات: {e}")
    exit()

# إعداد Flask وOpenAI
flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
client = OpenAI(api_key=OPENAI_API_KEY)

# متغيرات عامة
pending_messages = {}
timers = {}
thread_locks = {}
client_processing_locks = {}

processing_queue = deque()  # ✅ قائمة انتظار
is_processing = False       # ✅ حالة التتبع

# دوال الجلسات
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

# إرسال واتساب
def send_whatsapp_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        logger.info(f"📤 [WhatsApp] رسالة للعميل {phone} - حالة {response.status_code}")
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ WhatsApp إرسال: {e}")

# تحويل صوت
def transcribe_audio(audio_url, file_format="ogg"):
    logger.info(f"🎙 تحميل صوت من: {audio_url}")
    try:
        audio_response = requests.get(audio_url, stream=True)
        audio_response.raise_for_status()
        temp_audio_file = f"temp_audio_{int(time.time())}.{file_format}"
        with open(temp_audio_file, "wb") as f:
            for chunk in audio_response.iter_content(chunk_size=8192):
                f.write(chunk)
        with open(temp_audio_file, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        os.remove(temp_audio_file)
        return transcription.text
    except Exception as e:
        logger.error(f"❌ تحويل الصوت: {e}")
        return None

# التواصل مع المساعد
def ask_assistant(content, sender_id, name=""):
    session = get_session(sender_id)
    if name and not session.get("name"):
        session["name"] = name
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    thread_id_str = str(session["thread_id"])
    if thread_id_str not in thread_locks:
        thread_locks[thread_id_str] = threading.Lock()
    with thread_locks[thread_id_str]:
        client.beta.threads.messages.create(thread_id=thread_id_str, role="user", content=content)
        run = client.beta.threads.runs.create(thread_id=thread_id_str, assistant_id=ASSISTANT_ID_PREMIUM)
        while run.status in ["queued", "in_progress"]:
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id_str, run_id=run.id)
        if run.status == "completed":
            messages = client.beta.threads.messages.list(thread_id=thread_id_str)
            reply = messages.data[0].content[0].text.value.strip()
            session["history"].append({"role": "user", "content": content})
            session["history"].append({"role": "assistant", "content": reply})
            session["history"] = session["history"][-10:]
            save_session(sender_id, session)
            return reply
        return "⚠ حصل خطأ، جرب تاني."

# واتساب Webhook
@flask_app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    sender = data.get("phone")
    if not sender:
        return jsonify({"status": "no sender"}), 400
    name = data.get("pushname", "")
    msg = data.get("text", {}).get("message")
    image_url = data.get("image", {}).get("imageUrl")
    audio_url = data.get("audio", {}).get("audioUrl")

    session = get_session(sender)
    session["last_message_time"] = datetime.utcnow().isoformat()
    save_session(sender, session)

    sender_str = str(sender)

    if audio_url:
        transcribed = transcribe_audio(audio_url)
        if transcribed:
            reply = ask_assistant(f"رسالة صوتية من العميل: {transcribed}", sender, name)
            send_whatsapp_message(sender, reply)

    elif image_url:
        caption = data.get("image", {}).get("caption", "")
        content = [{"type": "image_url", "image_url": {"url": image_url}}]
        if caption:
            content.append({"type": "text", "text": f"تعليق على الصورة: {caption}"})
        reply = ask_assistant(content, sender, name)
        send_whatsapp_message(sender, reply)

    elif msg:
        if sender_str not in pending_messages:
            pending_messages[sender_str] = []
        pending_messages[sender_str].append(msg)
        logger.info(f"📝 تم تخزين رسالة من {sender_str}: {msg}")

        if sender_str not in timers:
            processing_queue.append((sender_str, name))
            timers[sender_str] = True
            logger.info(f"⏳ أضيف {sender_str} إلى قائمة الانتظار")

    return jsonify({"status": "received"}), 200

# معالجة عميل
def process_whatsapp_messages(sender, name):
    sender_str = str(sender)
    with client_processing_locks.setdefault(sender_str, threading.Lock()):
        logger.info(f"🚀 بدأ الرد على: {sender_str}")
        time.sleep(15)
        if not pending_messages.get(sender_str):
            logger.warning(f"❌ لا يوجد رسائل للعميل: {sender_str}")
            timers.pop(sender_str, None)
            return
        combined = "
".join(pending_messages[sender_str])
        logger.info(f"📦 الرسائل:
{combined}")
        reply = ask_assistant(combined, sender_str, name)
        time.sleep(3)
        send_whatsapp_message(sender_str, reply)
        time.sleep(1)
        pending_messages[sender_str] = []
        timers.pop(sender_str, None)
        logger.info(f"✅ الرد على {sender_str} انتهى")

# جدولة عميل كل 8 ثواني
def process_next_client():
    global is_processing
    if is_processing or not processing_queue:
        logger.info("⏳ لا معالجة الآن")
        return
    sender_str, name = processing_queue.popleft()
    logger.info(f"⏭ جارِ معالجة {sender_str}")
    is_processing = True
    try:
        process_whatsapp_messages(sender_str, name)
    finally:
        is_processing = False

# بوت تيليجرام كما هو (ما تمش تعديله)
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
@flask_app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def telegram_webhook_handler():
    data = request.get_json()
    await telegram_app.process_update(telegram.Update.de_json(data, telegram_app.bot))
    return jsonify({"status": "ok"})

# التهيئة
@flask_app.route("/")
def home():
    return "✅ Bot is running"

async def setup_telegram():
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if host:
        await telegram_app.initialize()
        await telegram_app.bot.set_webhook(
            url=f"https://{host}/{TELEGRAM_BOT_TOKEN}",
            allowed_updates=telegram.Update.ALL_TYPES
        )
        logger.info(f"✅ Webhook تيليجرام على: https://{host}/{TELEGRAM_BOT_TOKEN}")

try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(setup_telegram())
    else:
        asyncio.run(setup_telegram())
except Exception as e:
    logger.critical(f"❌ تيليجرام: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(process_next_client, 'interval', seconds=8)  # ✅ عميل كل 8 ثواني
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=True)
