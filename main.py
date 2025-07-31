import os
import time
import json
import requests
import threading
import asyncio
import logging
import queue
from flask import Flask, request, jsonify
from asgiref.wsgi import WsgiToAsgi
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# إعداد اللوج
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

# تحميل البيئة
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
    logger.info("✅ تم الاتصال بقاعدة البيانات بنجاح.")
except Exception as e:
    logger.critical(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    exit()

# Flask و OpenAI
flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
client = OpenAI(api_key=OPENAI_API_KEY)

# متغيرات عامة
thread_locks = {}
client_processing_locks = {}
whatsapp_queue = queue.Queue()

# إدارة الجلسات
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
        logger.info(f"📤 [WhatsApp] تم إرسال رسالة للعميل {phone}، الحالة: {response.status_code}")
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [WhatsApp] خطأ أثناء إرسال الرسالة عبر ZAPI: {e}")

# إرسال تيليجرام
async def send_telegram_message(context, chat_id, message, business_connection_id=None):
    try:
        if business_connection_id:
            await context.bot.send_message(chat_id=chat_id, text=message, business_connection_id=business_connection_id)
        else:
            await context.bot.send_message(chat_id=chat_id, text=message)
        logger.info(f"📤 Sent to Telegram user {chat_id}")
    except Exception as e:
        logger.error(f"❌ Telegram send error: {e}")

# تحويل الصوت
def transcribe_audio(audio_url, file_format="ogg"):
    logger.info(f"🎙 محاولة تحميل وتحويل الصوت من: {audio_url}")
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
        logger.error(f"❌ خطأ أثناء تحويل الصوت إلى نص: {e}")
        return None

# التفاعل مع المساعد
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

# دالة معالجة طابور واتساب
def process_whatsapp_queue():
    while True:
        if not whatsapp_queue.empty():
            item = whatsapp_queue.get()
            sender = item["sender"]
            name = item["name"]
            msg = item["msg"]
            image_url = item["image_url"]
            audio_url = item["audio_url"]
            caption = item["caption"]

            logger.info(f"🛠 [QUEUE] جاري معالجة رسالة من {sender}")

            reply = None
            if audio_url:
                transcribed_text = transcribe_audio(audio_url)
                if transcribed_text:
                    reply = ask_assistant(f"رسالة صوتية من العميل: {transcribed_text}", sender, name)
            elif image_url:
                content = [{"type": "image_url", "image_url": {"url": image_url}}]
                if caption:
                    content.append({"type": "text", "text": f"تعليق على الصورة: {caption}"})
                reply = ask_assistant(content, sender, name)
            elif msg:
                reply = ask_assistant(msg, sender, name)

            if reply:
                send_whatsapp_message(sender, reply)

            time.sleep(2)
        else:
            time.sleep(1)

# Webhook واتساب
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
    caption = data.get("image", {}).get("caption", "")

    whatsapp_queue.put({
        "sender": sender,
        "name": name,
        "msg": msg,
        "image_url": image_url,
        "caption": caption,
        "audio_url": audio_url
    })
    logger.info(f"📥 [QUEUE] أضيفت رسالة من {sender} إلى الطابور.")
    return jsonify({"status": "queued"}), 200

# Telegram
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

async def start_command(update, context):
    await update.message.reply_text(f"أهلاً {update.effective_user.first_name}!")

async def handle_telegram_message(update, context):
    message = update.message or update.business_message
    if not message:
        return
    chat_id = message.chat.id
    user_name = message.from_user.first_name
    business_id = getattr(update.business_message, "business_connection_id", None) if hasattr(update, "business_message") else None
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
        content_for_assistant = f"رسالة صوتية من العميل: {transcribed_text}" if transcribed_text else ""
    elif message.photo:
        photo_file = await message.photo[-1].get_file()
        caption = message.caption or ""
        content_list = [{"type": "image_url", "image_url": {"url": photo_file.file_path}}]
        if caption:
            content_list.append({"type": "text", "text": f"تعليق على الصورة: {caption}"})
        content_for_assistant = content_list
    if content_for_assistant and not reply_text:
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

# شغّل طابور واتساب
threading.Thread(target=process_whatsapp_queue, daemon=True).start()

# جدولة
scheduler = BackgroundScheduler()
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=True)
