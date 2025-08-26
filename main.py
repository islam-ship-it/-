# -*- coding: utf-8 -*-
import os
import time
import json
import requests
import threading
import asyncio
import logging
import base64
from flask import Flask, request, jsonify
from asgiref.wsgi import WsgiToAsgi
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# --- الإعدادات ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)
load_dotenv()

# --- مفاتيح API (النسخة النهائية المبسطة) ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
MONGO_URI = os.getenv("MONGO_URI")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

# --- قاعدة البيانات ---
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ تم الاتصال بقاعدة البيانات بنجاح.")
except Exception as e:
    logger.critical(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    exit()

# --- إعدادات التطبيق ---
flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
client = OpenAI(api_key=OPENAI_API_KEY)

# --- متغيرات عالمية لتجميع الرسائل ---
pending_messages = {}
message_timers = {}
processing_locks = {}
BATCH_WAIT_TIME = 10.0

# --- دوال إدارة الجلسات ---
def get_session(user_id):
    user_id_str = str(user_id)
    session = sessions_collection.find_one({"_id": user_id_str})
    if not session:
        logger.info(f"Creating new session for user_id: {user_id_str}")
        session = {
            "_id": user_id_str, "history": [], "thread_id": None,
            "message_count": 0, "name": "", "last_message_time": datetime.utcnow().isoformat(),
            "payment_status": "pending"
        }
    return session

def save_session(user_id, session_data):
    user_id_str = str(user_id)
    session_data["_id"] = user_id_str
    sessions_collection.replace_one({"_id": user_id_str}, session_data, upsert=True)

# --- دوال الإرسال ---
def send_manychat_reply(subscriber_id, text_message):
    if not MANYCHAT_API_KEY:
        logger.error("❌ [ManyChat API] MANYCHAT_API_KEY is not set. Cannot send message.")
        return
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    payload = {"subscriber_id": str(subscriber_id ), "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message}]}}}
    logger.info(f"📤 [ManyChat API] Sending reply to subscriber {subscriber_id}...")
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        logger.info(f"✅ [ManyChat API] تم إرسال الرسالة إلى {subscriber_id} بنجاح: {response.json()}")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [ManyChat API] فشل إرسال الرسالة: {e.response.text if e.response else e}")

async def send_telegram_message(bot, chat_id, text, business_id=None):
    try:
        if business_id:
            await bot.send_message(chat_id=chat_id, text=text, business_connection_id=business_id)
        else:
            await bot.send_message(chat_id=chat_id, text=text)
        logger.info(f"✅ [Telegram] تم إرسال الرسالة إلى {chat_id} بنجاح.")
    except Exception as e:
        logger.error(f"❌ [Telegram] فشل إرسال الرسالة إلى {chat_id}: {e}")

# --- دوال معالجة الوسائط والذكاء الاصطناعي ---
def download_media_from_url(media_url, headers=None):
    logger.info(f"⬇️ [Media Downloader] Attempting to download from URL: {media_url}")
    try:
        media_response = requests.get(media_url, headers=headers, timeout=20)
        media_response.raise_for_status()
        logger.info(f"✅ [Media Downloader] Successfully downloaded media content.")
        return media_response.content
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Media Downloader] فشل تحميل الوسائط من الرابط {media_url}: {e}")
        return None

def transcribe_audio(audio_content, file_format="mp4"):
    logger.info(f"🎙️ [Whisper] Transcribing audio (format: {file_format})...")
    try:
        temp_audio_file = f"temp_audio_{int(time.time())}.{file_format}"
        with open(temp_audio_file, "wb") as f: f.write(audio_content)
        with open(temp_audio_file, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        os.remove(temp_audio_file)
        logger.info(f"✅ [Whisper] Transcription successful: '{transcription.text}'")
        return transcription.text
    except Exception as e:
        logger.error(f"❌ [Whisper] خطأ أثناء تحويل الصوت إلى نص: {e}")
        return None

async def ask_assistant(content, sender_id, name=""):
    logger.info(f"🤖 [Assistant] Preparing request for sender_id: {sender_id}")
    session = get_session(sender_id)
    if name and not session.get("name"): session["name"] = name
    if not session.get("thread_id"):
        logger.info(f"🤖 [Assistant] No thread found for {sender_id}. Creating a new one.")
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id
    thread_id_str = str(session["thread_id"])
    if isinstance(content, str): content = [{"type": "text", "text": content}]
    try:
        client.beta.threads.messages.create(thread_id=thread_id_str, role="user", content=content)
        run = client.beta.threads.runs.create(thread_id=thread_id_str, assistant_id=ASSISTANT_ID_PREMIUM)
        start_time = time.time()
        while run.status in ["queued", "in_progress"]:
            if time.time() - start_time > 90:
                logger.error(f"Timeout waiting for run {run.id} to complete.")
                return "⚠️ حدث تأخير في الرد، يرجى المحاولة مرة أخرى."
            await asyncio.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id_str, run_id=run.id)
        if run.status == "completed":
            messages = client.beta.threads.messages.list(thread_id=thread_id_str, limit=1)
            reply = messages.data[0].content[0].text.value.strip()
            save_session(sender_id, session)
            return reply
        else:
            logger.error(f"❌ [Assistant] Run did not complete. Final Status: {run.status}")
            if run.last_error: logger.error(f"❌ [Assistant] Last Error: {run.last_error.message}")
            return "⚠️ عفوًا، حدث خطأ فني. فريقنا يعمل على إصلاحه."
    except Exception as e:
        logger.error(f"❌ [Assistant] An exception occurred: {e}", exc_info=True)
        return "⚠️ عفوًا، حدث خطأ غير متوقع."

# --- آلية التجميع الموحدة ---
def process_batched_messages_universal(sender_id):
    lock = processing_locks.setdefault(sender_id, threading.Lock())
    with lock:
        if sender_id not in pending_messages or not pending_messages[sender_id]:
            return
        user_data = pending_messages[sender_id]
        combined_content = "\n".join(user_data["texts"])
        platform = user_data["platform"]
        user_name = user_data["name"]
        logger.info(f"Processing batched messages for {sender_id} on {platform}. Content: '{combined_content}'")
        reply_text = asyncio.run(ask_assistant(combined_content, sender_id, user_name))
        if reply_text:
            if platform == "ManyChat":
                send_manychat_reply(sender_id, reply_text)
            elif platform == "Telegram":
                bot_instance = telegram_app.bot
                business_id = user_data.get("business_id")
                asyncio.run(send_telegram_message(bot_instance, sender_id, reply_text, business_id))
        del pending_messages[sender_id]
        if sender_id in message_timers:
            del message_timers[sender_id]

def handle_text_message(sender_id, text, platform, user_name, **kwargs):
    if sender_id in message_timers:
        message_timers[sender_id].cancel()
    if sender_id not in pending_messages:
        pending_messages[sender_id] = {"texts": [], "platform": platform, "name": user_name, **kwargs}
    pending_messages[sender_id]["texts"].append(text)
    logger.info(f"Message from {sender_id} on {platform} added to batch. Current batch size: {len(pending_messages[sender_id]['texts'])}")
    timer = threading.Timer(BATCH_WAIT_TIME, process_batched_messages_universal, args=[sender_id])
    message_timers[sender_id] = timer
    timer.start()

# --- معالجة الوسائط الفورية ---
def process_media_message_immediately(sender_id, user_name, platform, content_for_assistant, **kwargs):
    def target():
        logger.info(f"Processing media immediately for {sender_id} on {platform}.")
        reply_text = asyncio.run(ask_assistant(content_for_assistant, sender_id, user_name))
        if reply_text:
            if platform == "ManyChat":
                send_manychat_reply(sender_id, reply_text)
            elif platform == "Telegram":
                bot_instance = telegram_app.bot
                business_id = kwargs.get("business_id")
                asyncio.run(send_telegram_message(bot_instance, sender_id, reply_text, business_id))
    
    thread = threading.Thread(target=target)
    thread.start()

# --- ويب هوك ManyChat (نقطة الدخول الوحيدة لفيسبوك وانستغرام) ---
@flask_app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    auth_header = request.headers.get('Authorization')
    if not MANYCHAT_SECRET_KEY or auth_header != f'Bearer {MANYCHAT_SECRET_KEY}':
        logger.warning(f"🚨 [ManyChat Webhook] UNAUTHORIZED ACCESS ATTEMPT! 🚨")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    logger.info("✅ [ManyChat Webhook] Authorization successful.")
    data = request.get_json()
    
    full_contact = data.get("full_contact")
    
    if not full_contact:
        logger.error(f"[ManyChat Webhook] CRITICAL: 'full_contact' data not found in the request body. Data: {data}")
        return jsonify({"status": "error", "message": "'full_contact' data is required."}), 400

    sender_id = full_contact.get("id")
    user_name = full_contact.get("first_name", "User")
    last_input = full_contact.get("last_text_input") or full_contact.get("last_input_text")

    if not sender_id or not last_input:
        logger.warning(f"[ManyChat BG] Missing sender_id or last_input within full_contact. Data: {full_contact}")
        return jsonify({"status": "error", "message": "Missing critical data within full_contact"}), 400

    is_url = last_input.startswith(("http://", "https://" ))
    is_media_url = is_url and (any(ext in last_input for ext in ['.jpg', '.jpeg', '.png', '.gif', '.mp4', '.mp3', '.ogg']) or "cdn.fbsbx.com" in last_input or "scontent" in last_input)

    if is_media_url:
        logger.info(f"Handling media URL immediately for ManyChat: {last_input}")
        media_content = download_media_from_url(last_input)
        if media_content:
            content_for_assistant = None
            is_audio = any(ext in last_input for ext in ['.mp4', '.mp3', '.ogg']) or "audioclip" in last_input
            if is_audio:
                transcribed_text = transcribe_audio(media_content, file_format="mp4")
                if transcribed_text:
                    content_for_assistant = f"العميل أرسل رسالة صوتية، هذا هو نصها: \"{transcribed_text}\""
            else:
                base64_image = base64.b64encode(media_content).decode('utf-8')
                content_for_assistant = [{"type": "text", "text": "صف هذه الصورة باختصار شديد باللغة العربية."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]
            if content_for_assistant:
                process_media_message_immediately(sender_id, user_name, "ManyChat", content_for_assistant)
    else:
        handle_text_message(sender_id, last_input, "ManyChat", user_name)

    return jsonify({"status": "received"}), 200

# --- منطق تيليجرام ---
if TELEGRAM_BOT_TOKEN:
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    async def start_command(update, context):
        await update.message.reply_text(f"أهلاً {update.effective_user.first_name}!")

    async def handle_telegram_message(update, context):
        message = update.message or update.business_message
        if not message: return
        
        chat_id = str(message.chat.id)
        user_name = message.from_user.first_name
        business_id = getattr(update.business_message, "business_connection_id", None) if hasattr(update, "business_message") and update.business_message else None
        
        if message.text:
            handle_text_message(chat_id, message.text, "Telegram", user_name, business_id=business_id)
        else:
            content_for_assistant = None
            if message.voice:
                voice_file = await message.voice.get_file()
                voice_content = await voice_file.download_as_bytearray()
                transcribed_text = transcribe_audio(bytes(voice_content), file_format="ogg")
                if transcribed_text:
                    content_for_assistant = f"رسالة صوتية: {transcribed_text}"
            elif message.photo:
                caption = message.caption or ""
                photo_file = await message.photo[-1].get_file()
                photo_content = await photo_file.download_as_bytearray()
                base64_image = base64.b64encode(bytes(photo_content)).decode('utf-8')
                content_for_assistant = [{"type": "text", "text": f"هذه صورة أرسلها العميل. التعليق عليها هو: '{caption}'. قم بوصف الصورة والرد على التعليق."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]
            
            if content_for_assistant:
                process_media_message_immediately(chat_id, user_name, "Telegram", content_for_assistant, business_id=business_id)

    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_telegram_message))

    @flask_app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
    async def telegram_webhook_handler():
        data = request.get_json()
        update = telegram.Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return jsonify({"status": "ok"})

# --- الإعداد والتشغيل ---
@flask_app.route("/")
def home():
    return "✅ Bot is running with ManyChat & Telegram integrations ONLY. Simplified Edition."

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل عبر خادم WSGI (مثل Gunicorn).")
