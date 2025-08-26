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
from pymongo import MongoClient, ReturnDocument
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
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
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

# --- قاعدة البيانات ---
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    outgoing_collection = db["outgoing_whatsapp"]
    logger.info("✅ تم الاتصال بقاعدة البيانات بنجاح.")
except Exception as e:
    logger.critical(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    exit()

# --- إعدادات التطبيق ---
flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
client = OpenAI(api_key=OPENAI_API_KEY)

# --- متغيرات عالمية لتجميع الرسائل (الآلية الجديدة الموحدة) ---
pending_messages = {}
message_timers = {}
processing_locks = {}
BATCH_WAIT_TIME = 10.0 # مدة الانتظار بالثواني (يمكنك تعديلها)

# --- دوال إدارة الجلسات ودوال مساعدة ---
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

def _mask_token(tok: str):
    if not tok: return "None"
    return f"{tok[:6]}...{tok[-4:]}" if len(tok) > 12 else "****"

# --- دوال الإرسال ---
def send_meta_whatsapp_message(phone, message):
    url = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": phone, "text": {"body": message}}
    logger.info(f"📤 [Meta API] Preparing to send message to {phone}." )
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        logger.info(f"✅ [Meta API] تم إرسال الرسالة إلى {phone} بنجاح.")
        return True
    except requests.exceptions.RequestException as e:
        error_text = e.response.text if e.response else str(e)
        logger.error(f"❌ [Meta API] فشل إرسال الرسالة إلى {phone}: {error_text}")
        return False

def send_messenger_instagram_message(recipient_id, message, platform="Messenger"):
    token = PAGE_ACCESS_TOKEN if platform == "Messenger" else INSTAGRAM_ACCESS_TOKEN
    if not token:
        logger.warning(f"⚠️ [{platform}] Access token not set. Cannot send message.")
        return
    url = "https://graph.facebook.com/v19.0/me/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message}}
    logger.info(f"📤 [{platform}] Sending reply to {recipient_id} using token {_mask_token(token )}")
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        logger.info(f"✅ [{platform}] تم إرسال الرسالة إلى {recipient_id} بنجاح.")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [{platform}] فشل إرسال الرسالة: {e.response.text if e.response else e}")

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

def download_meta_media_by_id(media_id):
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
    url = f"https://graph.facebook.com/v19.0/{media_id}/"
    try:
        response = requests.get(url, headers=headers, timeout=20 )
        response.raise_for_status()
        media_info = response.json()
        media_url = media_info.get("url")
        return download_media_from_url(media_url, headers=headers)
    except requests.exceptions.RequestException:
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
            if platform == "WhatsApp":
                send_meta_whatsapp_message(sender_id, reply_text)
            elif platform in ["Messenger", "Instagram"]:
                send_messenger_instagram_message(sender_id, reply_text, platform)
            elif platform == "ManyChat":
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
            if platform == "WhatsApp":
                send_meta_whatsapp_message(sender_id, reply_text)
            elif platform == "ManyChat":
                send_manychat_reply(sender_id, reply_text)
            elif platform == "Telegram":
                bot_instance = telegram_app.bot
                business_id = kwargs.get("business_id")
                asyncio.run(send_telegram_message(bot_instance, sender_id, reply_text, business_id))
    
    thread = threading.Thread(target=target)
    thread.start()

# --- ويب هوك Meta (واتساب، ماسنجر، انستغرام) ---
@flask_app.route("/meta_webhook", methods=["GET", "POST"])
def meta_webhook():
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.challenge"):
            if not request.args.get("hub.verify_token") == META_VERIFY_TOKEN:
                return "Verification token mismatch", 403
            return request.args.get("hub.challenge"), 200
        return "Hello World", 200
    
    data = request.get_json()
    platform_object = data.get("object")

    if platform_object == "whatsapp_business_account":
        try:
            entry = data["entry"][0]
            change = entry["changes"][0]
            if change.get("field") != "messages": return "OK", 200
            value = change["value"]
            message = value["messages"][0]
            sender_id = message["from"]
            sender_name = value.get("contacts", [{}])[0].get("profile", {}).get("name", "")
            message_type = message["type"]

            if message_type == "text":
                handle_text_message(sender_id, message["text"]["body"], "WhatsApp", sender_name)
            elif message_type in ["image", "audio"]:
                content_for_assistant = None
                if message_type == "image":
                    image_id = message["image"]["id"]
                    image_content = download_meta_media_by_id(image_id)
                    if image_content:
                        base64_image = base64.b64encode(image_content).decode('utf-8')
                        content_for_assistant = [{"type": "text", "text": "صف هذه الصورة."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]
                elif message_type == "audio":
                    audio_id = message["audio"]["id"]
                    audio_content = download_meta_media_by_id(audio_id)
                    if audio_content:
                        transcribed_text = transcribe_audio(audio_content, file_format="ogg")
                        if transcribed_text:
                            content_for_assistant = f"رسالة صوتية: {transcribed_text}"
                if content_for_assistant:
                    process_media_message_immediately(sender_id, sender_name, "WhatsApp", content_for_assistant)
        except (IndexError, KeyError) as e:
            logger.warning(f"[Meta Webhook] Incomplete data received: {e}")
        except Exception as e:
            logger.error(f"❌ [WhatsApp Processor] Error: {e}", exc_info=True)

    elif platform_object in ["instagram", "page"]:
        try:
            platform_name = "Instagram" if platform_object == "instagram" else "Messenger"
            entry = data["entry"][0]
            messaging_event = entry["messaging"][0]
            sender_id = messaging_event["sender"]["id"]
            message_obj = messaging_event.get("message")
            if sender_id and message_obj and "text" in message_obj:
                handle_text_message(sender_id, message_obj["text"], platform_name, "User")
        except (IndexError, KeyError) as e:
            logger.warning(f"[Meta Webhook] Incomplete data received: {e}")
        except Exception as e:
            logger.error(f"❌ [Messenger/IG Processor] Error: {e}", exc_info=True)

    return "OK", 200

# --- ويب هوك ManyChat ---
@flask_app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    auth_header = request.headers.get('Authorization')
    if not MANYCHAT_SECRET_KEY or auth_header != f'Bearer {MANYCHAT_SECRET_KEY}':
        logger.warning(f"🚨 [ManyChat Webhook] UNAUTHORIZED ACCESS ATTEMPT! 🚨")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    data = request.get_json()
    contact_data = data.get("manychat_data", {})
    sender_id = contact_data.get("id")
    user_name = contact_data.get("first_name", "User")
    last_input = contact_data.get("last_input_text")

    if not sender_id or not last_input:
        return jsonify({"status": "error", "message": "Missing data"}), 400

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
    return "✅ Bot is running with Universal Batching Logic."

def process_db_queue():
    if not all([ZAPI_BASE_URL, ZAPI_INSTANCE_ID, ZAPI_TOKEN, CLIENT_TOKEN]): return
    try:
        message_to_send = outgoing_collection.find_one_and_update({"status": "pending"}, {"$set": {"status": "processing", "processed_at": datetime.utcnow()}}, sort=[("created_at", 1)], return_document=ReturnDocument.AFTER)
        if message_to_send:
            phone = message_to_send["phone"]
            message_text = message_to_send["message"]
            message_id = message_to_send["_id"]
            url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
            headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
            payload = {"phone": phone, "message": message_text}
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=20)
                response.raise_for_status()
                outgoing_collection.update_one({"_id": message_id}, {"$set": {"status": "sent", "sent_at": datetime.utcnow()}})
            except requests.exceptions.RequestException as e:
                outgoing_collection.update_one({"_id": message_id}, {"$set": {"status": "pending", "error_count": message_to_send.get("error_count", 0) + 1}})
    except Exception as e:
        logger.error(f"❌ [DB Queue Processor] Error: {e}")

if ZAPI_BASE_URL:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(func=process_db_queue, trigger="interval", seconds=15, id="db_queue_processor", replace_existing=True)
    scheduler.start()
    logger.info("🚀 تم تشغيل مجدول المهام (APScheduler) لمعالجة طابور رسائل ZAPI.")

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل عبر خادم WSGI (مثل Gunicorn).")
