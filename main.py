# -*- coding: utf-8 -*-
import os
import time
import json
import requests
import threading
import asyncio
import logging
import base64  # <-- إضافة مهمة لتحويل الصور
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
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

# --- التحقق من وجود المتغيرات الأساسية ---
if not all([OPENAI_API_KEY, ASSISTANT_ID_PREMIUM, MONGO_URI, META_ACCESS_TOKEN, META_PHONE_NUMBER_ID, META_VERIFY_TOKEN]):
    logger.critical("FATAL ERROR: One or more required environment variables are missing.")
    exit()

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

# --- متغيرات عالمية ---
thread_locks = {}

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

# --- دوال إرسال واتساب ---
def process_db_queue():
    if not all([ZAPI_BASE_URL, ZAPI_INSTANCE_ID, ZAPI_TOKEN, CLIENT_TOKEN]):
        return
    try:
        message_to_send = outgoing_collection.find_one_and_update(
            {"status": "pending"},
            {"$set": {"status": "processing", "processed_at": datetime.utcnow()}},
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER
        )
        if message_to_send:
            phone = message_to_send["phone"]
            message_text = message_to_send["message"]
            message_id = message_to_send["_id"]
            logger.info(f"📤 [DB Queue - ZAPI] تم سحب رسالة ({message_id}) للرقم {phone}.")
            url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
            headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
            payload = {"phone": phone, "message": message_text}
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=20)
                response.raise_for_status()
                outgoing_collection.update_one({"_id": message_id}, {"$set": {"status": "sent", "sent_at": datetime.utcnow()}})
            except requests.exceptions.RequestException as e:
                logger.error(f"❌ [ZAPI] فشل إرسال الرسالة ({message_id}): {e}")
                outgoing_collection.update_one({"_id": message_id}, {"$set": {"status": "pending", "error_count": message_to_send.get("error_count", 0) + 1}})
    except Exception as e:
        logger.error(f"❌ [DB Queue Processor] حدث خطأ جسيم: {e}")

def send_meta_whatsapp_message(phone, message):
    url = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": phone, "text": {"body": message}}
    logger.info(f"📤 [Meta API] Preparing to send message to {phone}. Payload: {json.dumps(payload )}")
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        logger.info(f"✅ [Meta API] تم إرسال الرسالة إلى {phone} بنجاح.")
        return True
    except requests.exceptions.RequestException as e:
        error_text = e.response.text if e.response else str(e)
        logger.error(f"❌ [Meta API] فشل إرسال الرسالة إلى {phone}: {error_text}")
        return False

# --- الدوال المشتركة ---
def download_meta_media(media_id):
    logger.info(f"⬇️ [Meta Media] Attempting to get URL for media_id: {media_id}")
    url = f"https://graph.facebook.com/v19.0/{media_id}/"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
    try:
        response = requests.get(url, headers=headers, timeout=20 )
        response.raise_for_status()
        media_info = response.json()
        media_url = media_info.get("url")
        media_response = requests.get(media_url, headers=headers, timeout=20)
        media_response.raise_for_status()
        logger.info(f"✅ [Meta Media] Successfully downloaded media content for ID: {media_id}")
        return media_response.content
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Meta Media] فشل تحميل الوسائط {media_id}: {e}")
        return None

def transcribe_audio(audio_content, file_format="ogg"):
    logger.info(f"🎙️ [Whisper] Transcribing audio...")
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
    if thread_id_str not in thread_locks: thread_locks[thread_id_str] = threading.Lock()
    with thread_locks[thread_id_str]:
        try:
            client.beta.threads.messages.create(thread_id=thread_id_str, role="user", content=content)
            run = client.beta.threads.runs.create(thread_id=thread_id_str, assistant_id=ASSISTANT_ID_PREMIUM)
            start_time = time.time()
            while run.status in ["queued", "in_progress"]:
                if time.time() - start_time > 60:
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

# --- منطق واتساب Meta Cloud API ---
@flask_app.route("/meta_webhook", methods=["GET", "POST"])
def meta_webhook():
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.challenge"):
            if not request.args.get("hub.verify_token") == META_VERIFY_TOKEN:
                return "Verification token mismatch", 403
            return request.args.get("hub.challenge"), 200
        return "Hello World", 200
    if request.method == "POST":
        data = request.json
        if data.get("object") == "whatsapp_business_account":
            thread = threading.Thread(target=process_meta_message, args=(data,))
            thread.start()
        return "OK", 200

def process_meta_message(data):
    try:
        entry = data.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        if change.get("field") != "messages": return
        value = change.get("value", {})
        message = value.get("messages", [{}])[0]
        if not message or "from" not in message: return
        sender_id = message.get("from")
        sender_name = value.get("contacts", [{}])[0].get("profile", {}).get("name", "")
        message_type = message.get("type")
        logger.info(f"📥 [Meta API] Processing message from {sender_id} ({sender_name}) | Type: {message_type}")
        session = get_session(sender_id)
        session["last_message_time"] = datetime.utcnow().isoformat()
        save_session(sender_id, session)
        content_for_assistant, reply_text = None, None
        if message_type == "text":
            content_for_assistant = message.get("text", {}).get("body")
        elif message_type == "image":
            caption = message.get("image", {}).get("caption", "")
            image_id = message.get("image", {}).get("id")
            image_content = download_meta_media(image_id)
            if image_content:
                base64_image = base64.b64encode(image_content).decode('utf-8')
                logger.info("👁️ [Vision API] Analyzing image content...")
                try:
                    vision_response = client.chat.completions.create(
                        model="gpt-4o",
                        messages=[{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "صف هذه الصورة باختصار شديد باللغة العربية. ما هو المنتج أو الشيء الرئيسي فيها؟"},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                            ]
                        }],
                        max_tokens=100
                    )
                    image_description = vision_response.choices[0].message.content
                    logger.info(f"👁️ [Vision API] Image description: {image_description}")
                    content_for_assistant = f"العميل أرسل صورة. وصف الصورة هو: '{image_description}'."
                    if caption: content_for_assistant += f" وكان التعليق عليها: \"{caption}\""
                except Exception as e:
                    logger.error(f"❌ [Vision API] فشل تحليل الصورة: {e}")
                    reply_text = "تم استلام الصورة، ولكن حدث خطأ أثناء تحليلها."
            else:
                reply_text = "عذراً، لم أتمكن من معالجة الصورة."
        elif message_type == "audio":
            audio_id = message.get("audio", {}).get("id")
            audio_content = download_meta_media(audio_id)
            if audio_content:
                transcribed_text = transcribe_audio(audio_content)
                if transcribed_text: content_for_assistant = f"رسالة صوتية من العميل: {transcribed_text}"
                else: reply_text = "عذراً، لم أتمكن من فهم رسالتك الصوتية."
            else:
                reply_text = "عذراً، لم أتمكن من معالجة الرسالة الصوتية."
        else:
            logger.info(f"Ignoring message type: {message_type}")
            return
        if content_for_assistant and not reply_text:
            reply_text = asyncio.run(ask_assistant(content_for_assistant, sender_id, sender_name))
        if reply_text:
            send_meta_whatsapp_message(sender_id, reply_text)
    except Exception as e:
        logger.error(f"❌ [Meta Webhook Processor] خطأ في معالجة الطلب: {e}", exc_info=True)

# --- منطق تيليجرام ---
if TELEGRAM_BOT_TOKEN:
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    async def start_command(update, context):
        await update.message.reply_text(f"أهلاً {update.effective_user.first_name}!")
    async def handle_telegram_message(update, context):
        message = update.message or update.business_message
        if not message: return
        chat_id = message.chat.id
        user_name = message.from_user.first_name
        business_id = getattr(update.business_message, "business_connection_id", None) if hasattr(update, "business_message") and update.business_message else None
        logger.info(f"📥 [Telegram] رسالة جديدة | Chat ID: {chat_id}, Name: {user_name}")
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING, business_connection_id=business_id)
        except Exception as e:
            logger.warning(f"⚠️ لم يتمكن من إرسال chat action: {e}")
        session = get_session(chat_id)
        session["last_message_time"] = datetime.utcnow().isoformat()
        save_session(chat_id, session)
        reply_text, content_for_assistant = "", None
        try:
            if message.text:
                content_for_assistant = message.text
            elif message.voice:
                voice_file = await message.voice.get_file()
                voice_content = await voice_file.download_as_bytearray()
                transcribed_text = transcribe_audio(bytes(voice_content))
                if transcribed_text: content_for_assistant = f"رسالة صوتية من العميل: {transcribed_text}"
                else: reply_text = "عذراً، لم أتمكن من فهم رسالتك الصوتية."
            elif message.photo:
                caption = message.caption or ""
                content_for_assistant = "العميل أرسل صورة."
                if caption: content_for_assistant += f" وكان التعليق عليها: \"{caption}\""
            if content_for_assistant and not reply_text:
                reply_text = await ask_assistant(content_for_assistant, chat_id, user_name)
            if reply_text:
                if business_id: await context.bot.send_message(chat_id=chat_id, text=reply_text, business_connection_id=business_id)
                else: await context.bot.send_message(chat_id=chat_id, text=reply_text)
        except Exception as e:
            logger.error(f"❌ [Telegram Handler] حدث خطأ أثناء معالجة الرسالة: {e}", exc_info=True)
            await context.bot.send_message(chat_id=chat_id, text="عذرًا، حدث خطأ غير متوقع.", business_connection_id=business_id)
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
    return "✅ Bot is running with Vision support"

async def setup_telegram_webhook():
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if host and TELEGRAM_BOT_TOKEN:
        webhook_url = f"https://{host}/{TELEGRAM_BOT_TOKEN}"
        await telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=telegram.Update.ALL_TYPES )
        logger.info(f"✅ تم إعداد Telegram Webhook على: {webhook_url}")

if ZAPI_BASE_URL:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(func=process_db_queue, trigger="interval", seconds=15, id="db_queue_processor", replace_existing=True)
    scheduler.start()
    logger.info("🚀 تم تشغيل مجدول المهام (APScheduler) لمعالجة طابور رسائل ZAPI.")

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل عبر خادم WSGI (مثل Gunicorn).")
