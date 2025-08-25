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
MESSENGER_ACCESS_TOKEN = os.getenv("MESSENGER_ACCESS_TOKEN") # <-- جديد: توكن لفيسبوك وانستغرام
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

# --- متغيرات عالمية لتجميع الرسائل ---
pending_whatsapp_messages = {}
whatsapp_timers = {}
processing_locks = {}

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
def send_meta_whatsapp_message(phone, message):
    url = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": phone, "text": {"body": message}}
    logger.info(f"📤 [Meta API] Preparing to send message to {phone}."  )
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        logger.info(f"✅ [Meta API] تم إرسال الرسالة إلى {phone} بنجاح.")
        return True
    except requests.exceptions.RequestException as e:
        error_text = e.response.text if e.response else str(e)
        logger.error(f"❌ [Meta API] فشل إرسال الرسالة إلى {phone}: {error_text}")
        return False

# <-- جديد: دالة إرسال جديدة لفيسبوك وانستغرام -->
def send_messenger_instagram_message(recipient_id, message):
    if not MESSENGER_ACCESS_TOKEN: return
    url = "https://graph.facebook.com/v19.0/me/messages"
    headers = {"Authorization": f"Bearer {MESSENGER_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"recipient": {"id": recipient_id}, "message": {"text": message}}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20 )
        response.raise_for_status()
        logger.info(f"✅ [Messenger/IG] تم إرسال الرسالة إلى {recipient_id} بنجاح.")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Messenger/IG] فشل إرسال الرسالة: {e.response.text if e.response else e}")

# --- الدوال المشتركة ---
def download_meta_media(media_id):
    logger.info(f"⬇️ [Meta Media] Attempting to get URL for media_id: {media_id}")
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"} # يستخدم توكن واتساب لأنه خاص بوسائط واتساب
    url = f"https://graph.facebook.com/v19.0/{media_id}/"
    try:
        response = requests.get(url, headers=headers, timeout=20  )
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

# --- دالة معالجة الرسائل المجمعة ---
def process_batched_messages(sender_id, sender_name):
    lock = processing_locks.setdefault(sender_id, threading.Lock())
    with lock:
        if sender_id not in pending_whatsapp_messages or not pending_whatsapp_messages[sender_id]:
            return
        logger.info(f"⏰ Timer finished for {sender_id}. Processing {len(pending_whatsapp_messages[sender_id])} batched messages.")
        combined_content = "\n".join(pending_whatsapp_messages[sender_id])
        logger.info(f"📝 Combined message for {sender_id}:\n--- START ---\n{combined_content}\n--- END ---")
        reply_text = asyncio.run(ask_assistant(combined_content, sender_id, sender_name))
        if reply_text:
            send_meta_whatsapp_message(sender_id, reply_text)
        del pending_whatsapp_messages[sender_id]
        if sender_id in whatsapp_timers:
            del whatsapp_timers[sender_id]

# --- منطق الويب هوك الرئيسي ---
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
        platform = data.get("object") # <-- جديد: تحديد المنصة من هنا

        # <-- جديد: توجيه الرسالة بناءً على المنصة -->
        if platform == "whatsapp_business_account":
            thread = threading.Thread(target=process_whatsapp_message, args=(data,))
            thread.start()
        elif platform == "instagram" or platform == "page": # "page" هو الاسم الذي تستخدمه Meta للماسنجر
            thread = threading.Thread(target=process_messenger_instagram_message, args=(data,))
            thread.start()
            
        return "OK", 200

# --- دوال معالجة الرسائل لكل منصة ---

def process_whatsapp_message(data):
    # هذه الدالة تبقى كما هي تمامًا، لا تغيير فيها
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
        if message_type == "text":
            text_body = message.get("text", {}).get("body")
            logger.info(f"💬 [WhatsApp] Received text from {sender_id}: '{text_body}'")
            if sender_id in whatsapp_timers: whatsapp_timers[sender_id].cancel()
            if sender_id not in pending_whatsapp_messages: pending_whatsapp_messages[sender_id] = []
            pending_whatsapp_messages[sender_id].append(text_body)
            logger.info(f"📥 Message from {sender_id} added to batch. Current batch size: {len(pending_whatsapp_messages[sender_id])}")
            timer = threading.Timer(15.0, process_batched_messages, args=[sender_id, sender_name])
            whatsapp_timers[sender_id] = timer
            timer.start()
        else:
            media_thread = threading.Thread(target=process_single_whatsapp_message, args=(data,))
            media_thread.start()
    except Exception as e:
        logger.error(f"❌ [WhatsApp Processor] Error: {e}", exc_info=True)

def process_single_whatsapp_message(data):
    # هذه الدالة تبقى كما هي تمامًا، لا تغيير فيها
    try:
        entry = data.get("entry", [])[0]
        value = entry.get("changes", [])[0].get("value", {})
        message = value.get("messages", [{}])[0]
        sender_id = message.get("from")
        sender_name = value.get("contacts", [{}])[0].get("profile", {}).get("name", "")
        message_type = message.get("type")
        logger.info(f"🖼️ [WhatsApp] Received non-text message of type '{message_type}' from {sender_id}")
        content_for_assistant, reply_text = None, None
        if message_type == "image":
            caption = message.get("image", {}).get("caption", "")
            image_id = message.get("image", {}).get("id")
            image_content = download_meta_media(image_id)
            if image_content:
                base64_image = base64.b64encode(image_content).decode('utf-8')
                try:
                    vision_response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": [{"type": "text", "text": "صف هذه الصورة باختصار شديد باللغة العربية."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}], max_tokens=100)
                    image_description = vision_response.choices[0].message.content
                    content_for_assistant = f"العميل أرسل صورة. وصفها: '{image_description}'."
                    if caption: content_for_assistant += f" تعليقه: \"{caption}\""
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
        if content_for_assistant and not reply_text:
            reply_text = asyncio.run(ask_assistant(content_for_assistant, sender_id, sender_name))
        if reply_text:
            send_meta_whatsapp_message(sender_id, reply_text)
    except Exception as e:
        logger.error(f"❌ [Single Message Processor] خطأ في معالجة الطلب: {e}", exc_info=True)

# <-- جديد: دالة معالجة جديدة لرسائل فيسبوك وانستغرام -->
def process_messenger_instagram_message(data):
    try:
        platform_name = "Messenger" if data.get("object") == "page" else "Instagram"
        entry = data.get("entry", [])[0]
        messaging_event = entry.get("messaging", [{}])[0]
        
        sender_id = messaging_event.get("sender", {}).get("id")
        message_obj = messaging_event.get("message")

        if not sender_id or not message_obj or "text" not in message_obj:
            # تجاهل الأحداث التي ليست رسائل نصية (مثل seen)
            return

        text_body = message_obj.get("text")
        logger.info(f"💬 [{platform_name}] Received text from {sender_id}: '{text_body}'")

        # ملاحظة: لم نطبق تجميع الرسائل هنا بعد للتبسيط، يمكن إضافتها لاحقًا بنفس طريقة واتساب
        # حاليًا، الرد فوري
        reply_text = asyncio.run(ask_assistant(text_body, sender_id, "User"))
        
        if reply_text:
            send_messenger_instagram_message(sender_id, reply_text)

    except Exception as e:
        logger.error(f"❌ [Messenger/IG Processor] Error: {e}", exc_info=True)

# --- منطق تيليجرام ---
# (لا تغيير هنا، كل شيء كما هو)
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
                logger.info(f"💬 [Telegram] Received text from {chat_id}: '{message.text}'")
                content_for_assistant = message.text
            elif message.voice:
                logger.info(f"🎙️ [Telegram] Received voice message from {chat_id}")
                voice_file = await message.voice.get_file()
                voice_content = await voice_file.download_as_bytearray()
                transcribed_text = transcribe_audio(bytes(voice_content))
                if transcribed_text: content_for_assistant = f"رسالة صوتية من العميل: {transcribed_text}"
                else: reply_text = "عذراً، لم أتمكن من فهم رسالتك الصوتية."
            elif message.photo:
                logger.info(f"🖼️ [Telegram] Received photo from {chat_id}")
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
    return "✅ Bot is running with Vision and Batching support"

def process_db_queue():
    if not all([ZAPI_BASE_URL, ZAPI_INSTANCE_ID, ZAPI_TOKEN, CLIENT_TOKEN]): return
    try:
        message_to_send = outgoing_collection.find_one_and_update({"status": "pending"}, {"$set": {"status": "processing", "processed_at": datetime.utcnow()}}, sort=[("created_at", 1)], return_document=ReturnDocument.AFTER)
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

if ZAPI_BASE_URL:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(func=process_db_queue, trigger="interval", seconds=15, id="db_queue_processor", replace_existing=True)
    scheduler.start()
    logger.info("🚀 تم تشغيل مجدول المهام (APScheduler) لمعالجة طابور رسائل ZAPI.")

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل عبر خادم WSGI (مثل Gunicorn).")
