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
from datetime import datetime, timezone
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# --- الإعدادات ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)
load_dotenv()

# --- مفاتيح API ---
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

# --- دوال إدارة الجلسات (النسخة المصححة) ---
def get_or_create_session_from_contact(contact_data, platform):
    if platform in ["ManyChat-Instagram", "ManyChat-Facebook"]:
        user_id = str(contact_data.get("id"))
    elif platform == "Telegram":
        user_id = str(contact_data.get("id"))
    else:
        logger.error(f"Could not determine user_id for platform {platform}")
        return None

    session = sessions_collection.find_one({"_id": user_id})
    now_utc = datetime.now(timezone.utc)

    main_platform = "Unknown"
    if platform == "ManyChat-Instagram":
        main_platform = "Instagram"
    elif platform == "ManyChat-Facebook":
        main_platform = "Facebook"
    elif platform == "Telegram":
        main_platform = "Telegram"

    if session:
        update_fields = {
            "last_contact_date": now_utc,
            "platform": main_platform, # <-- هذا هو السطر الذي تم إضافته للتصحيح
            "profile.name": contact_data.get("name"),
            "profile.profile_pic": contact_data.get("profile_pic"),
            "status": "active"
        }
        update_fields = {k: v for k, v in update_fields.items() if v is not None}
        sessions_collection.update_one({"_id": user_id}, {"$set": update_fields})
        logger.info(f"Updated session for existing user: {user_id} on platform {platform}")
        session = sessions_collection.find_one({"_id": user_id})
    else:
        logger.info(f"Creating new comprehensive session for user: {user_id} on platform {platform}")
        new_session = {
            "_id": user_id,
            "platform": main_platform,
            "profile": {
                "name": contact_data.get("name"),
                "first_name": contact_data.get("first_name"),
                "last_name": contact_data.get("last_name"),
                "profile_pic": contact_data.get("profile_pic")
            },
            "openai_thread_id": None,
            "tags": [f"source:{main_platform.lower()}"],
            "custom_fields": contact_data.get("custom_fields", {}),
            "conversation_summary": "",
            "status": "active",
            "first_contact_date": now_utc,
            "last_contact_date": now_utc
        }
        sessions_collection.insert_one(new_session)
        session = new_session
        logger.info(f"Successfully created new session for {user_id}")

    return session

async def get_assistant_reply(session, content):
    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")

    if not thread_id:
        logger.info(f"🤖 [Assistant] No thread found for {user_id}. Creating a new one.")
        thread = client.beta.threads.create()
        thread_id = thread.id
        sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
    
    if isinstance(content, str): content = [{"type": "text", "text": content}]

    try:
        client.beta.threads.messages.create(thread_id=thread_id, role="user", content=content)
        run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=ASSISTANT_ID_PREMIUM)
        start_time = time.time()
        while run.status in ["queued", "in_progress"]:
            if time.time() - start_time > 90:
                logger.error(f"Timeout waiting for run {run.id} to complete.")
                return "⚠️ حدث تأخير في الرد، يرجى المحاولة مرة أخرى."
            await asyncio.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run.status == "completed":
            messages = client.beta.threads.messages.list(thread_id=thread_id, limit=1)
            return messages.data[0].content[0].text.value.strip()
        else:
            logger.error(f"❌ [Assistant] Run did not complete. Status: {run.status}. Error: {run.last_error}")
            return "⚠️ عفوًا، حدث خطأ فني. فريقنا يعمل على إصلاحه."
    except Exception as e:
        logger.error(f"❌ [Assistant] An exception occurred: {e}", exc_info=True)
        return "⚠️ عفوًا، حدث خطأ غير متوقع."

# --- دوال الإرسال والوسائط (بدون تغيير) ---
def send_manychat_reply(subscriber_id, text_message):
    if not MANYCHAT_API_KEY:
        logger.error("❌ [ManyChat API] MANYCHAT_API_KEY is not set.")
        return
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    payload = {"subscriber_id": str(subscriber_id ), "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message}]}}}
    logger.info(f"📤 [ManyChat API] Sending reply to subscriber {subscriber_id}...")
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        logger.info(f"✅ [ManyChat API] Message sent successfully to {subscriber_id}.")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [ManyChat API] Failed to send message: {e.response.text if e.response else e}")

async def send_telegram_message(bot, chat_id, text, business_id=None):
    try:
        if business_id:
            await bot.send_message(chat_id=chat_id, text=text, business_connection_id=business_id)
        else:
            await bot.send_message(chat_id=chat_id, text=text)
        logger.info(f"✅ [Telegram] Message sent successfully to {chat_id}.")
    except Exception as e:
        logger.error(f"❌ [Telegram] Failed to send message to {chat_id}: {e}")

def download_media_from_url(media_url, headers=None):
    logger.info(f"⬇️ [Media Downloader] Attempting to download from URL: {media_url}")
    try:
        media_response = requests.get(media_url, headers=headers, timeout=20)
        media_response.raise_for_status()
        return media_response.content
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Media Downloader] Failed to download media from {media_url}: {e}")
        return None

def transcribe_audio(audio_content, file_format="mp4"):
    logger.info(f"🎙️ [Whisper] Transcribing audio (format: {file_format})...")
    try:
        temp_audio_file = f"temp_audio_{int(time.time())}.{file_format}"
        with open(temp_audio_file, "wb") as f: f.write(audio_content)
        with open(temp_audio_file, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        os.remove(temp_audio_file)
        return transcription.text
    except Exception as e:
        logger.error(f"❌ [Whisper] Error during transcription: {e}")
        return None

# --- آلية التجميع والمعالجة الموحدة (مُحسّنة) ---
def process_batched_messages_universal(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages or not pending_messages[user_id]:
            return
        
        user_data = pending_messages[user_id]
        session = user_data["session"]
        platform = session["platform"]
        combined_content = "\n".join(user_data["texts"])
        
        logger.info(f"Processing batched messages for {user_id} on {platform}. Content: '{combined_content}'")
        reply_text = asyncio.run(get_assistant_reply(session, combined_content))
        
        if reply_text:
            if platform in ["Instagram", "Facebook"]:
                send_manychat_reply(user_id, reply_text)
            elif platform == "Telegram":
                bot_instance = telegram_app.bot
                business_id = user_data.get("business_id")
                asyncio.run(send_telegram_message(bot_instance, user_id, reply_text, business_id))
        
        del pending_messages[user_id]
        if user_id in message_timers:
            del message_timers[user_id]

def handle_text_message(session, text, **kwargs):
    user_id = session["_id"]
    if user_id in message_timers:
        message_timers[user_id].cancel()
    
    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session, **kwargs}
    
    pending_messages[user_id]["texts"].append(text)
    logger.info(f"Message from {user_id} on {session['platform']} added to batch. Current batch size: {len(pending_messages[user_id]['texts'])}")
    
    timer = threading.Timer(BATCH_WAIT_TIME, process_batched_messages_universal, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

def process_media_message_immediately(session, content_for_assistant, **kwargs):
    def target():
        user_id = session["_id"]
        platform = session["platform"]
        logger.info(f"Processing media immediately for {user_id} on {platform}.")
        reply_text = asyncio.run(get_assistant_reply(session, content_for_assistant))
        
        if reply_text:
            if platform in ["Instagram", "Facebook"]:
                send_manychat_reply(user_id, reply_text)
            elif platform == "Telegram":
                bot_instance = telegram_app.bot
                business_id = kwargs.get("business_id")
                asyncio.run(send_telegram_message(bot_instance, user_id, reply_text, business_id))
    
    thread = threading.Thread(target=target)
    thread.start()

# --- ويب هوك ManyChat (مُطوّر) ---
@flask_app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    auth_header = request.headers.get('Authorization')
    if not MANYCHAT_SECRET_KEY or auth_header != f'Bearer {MANYCHAT_SECRET_KEY}':
        logger.warning(f"🚨 [ManyChat Webhook] UNAUTHORIZED ACCESS ATTEMPT! 🚨")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    data = request.get_json()
    full_contact = data.get("full_contact")
    
    if not full_contact:
        logger.error(f"[ManyChat Webhook] CRITICAL: 'full_contact' data not found. Data: {data}")
        return jsonify({"status": "error", "message": "'full_contact' data is required."}), 400

    platform = "ManyChat-Instagram" if full_contact.get("ig_id") else "ManyChat-Facebook"
    session = get_or_create_session_from_contact(full_contact, platform)
    
    if not session:
        return jsonify({"status": "error", "message": "Failed to create or get session"}), 500

    last_input = full_contact.get("last_text_input") or full_contact.get("last_input_text")
    if not last_input:
        return jsonify({"status": "received", "message": "No text input to process"}), 200

    is_url = last_input.startswith(("http://", "https://" ))
    is_media_url = is_url and (any(ext in last_input for ext in ['.jpg', '.jpeg', '.png', '.gif', '.mp4', '.mp3', '.ogg']) or "cdn.fbsbx.com" in last_input or "scontent" in last_input)

    if is_media_url:
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
                process_media_message_immediately(session, content_for_assistant)
    else:
        handle_text_message(session, last_input)

    return jsonify({"status": "received"}), 200

# --- منطق تيليجرام (مُطوّر) ---
if TELEGRAM_BOT_TOKEN:
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    async def start_command(update, context):
        await update.message.reply_text(f"أهلاً {update.effective_user.first_name}!")

    async def handle_telegram_message(update, context):
        message = update.message or update.business_message
        if not message: return
        
        user_contact_data = {
            "id": message.from_user.id,
            "name": message.from_user.full_name,
            "first_name": message.from_user.first_name,
            "last_name": message.from_user.last_name,
        }
        session = get_or_create_session_from_contact(user_contact_data, "Telegram")
        if not session: return

        business_id = getattr(update.business_message, "business_connection_id", None) if hasattr(update, "business_message") and update.business_message else None
        
        if message.text:
            handle_text_message(session, message.text, business_id=business_id)
        else:
            content_for_assistant = None
            if message.voice:
                voice_file = await message.voice.get_file()
                voice_content = await voice_file.download_as_bytearray()
                transcribed_text = transcribe_audio(bytes(voice_content), file_format="ogg")
                if transcribed_text: content_for_assistant = f"رسالة صوتية: {transcribed_text}"
            elif message.photo:
                caption = message.caption or ""
                photo_file = await message.photo[-1].get_file()
                photo_content = await photo_file.download_as_bytearray()
                base64_image = base64.b64encode(bytes(photo_content)).decode('utf-8')
                content_for_assistant = [{"type": "text", "text": f"هذه صورة أرسلها العميل. التعليق عليها هو: '{caption}'. قم بوصف الصورة والرد على التعليق."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]
            
            if content_for_assistant:
                process_media_message_immediately(session, content_for_assistant, business_id=business_id)

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
    return "✅ Bot is running with Advanced MongoDB Logging (v2 - Patched)."

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل عبر خادم WSGI (مثل Gunicorn).")
