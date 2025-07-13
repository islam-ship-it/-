import os
import time
import json
import requests
import threading
import traceback
import random
import asyncio
import logging
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# Telegram imports
import telegram
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    BusinessConnectionHandler,
    BusinessMessageHandler,
    EditedBusinessMessageHandler,
    Update,
)
from telegram.error import TelegramError

# ==============================================================================
# 1. الإعدادات الأساسية وتحميل متغيرات البيئة
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# ==============================================================================
# 2. إعدادات البيئة
# ==============================================================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
PORT = int(os.environ.get("PORT", 5000))

# بناء Webhook URL بأمان
if RENDER_EXTERNAL_HOSTNAME and TELEGRAM_BOT_TOKEN:
    WEBHOOK_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}/{TELEGRAM_BOT_TOKEN}"
else:
    WEBHOOK_URL = None
    logger.warning("⚠️ متغيرات البيئة اللازمة للـ Webhook غير مكتملة." )

# ==============================================================================
# 3. التحقق من المتغيرات الأساسية
# ==============================================================================
if not all([OPENAI_API_KEY, ASSISTANT_ID_PREMIUM, TELEGRAM_BOT_TOKEN, MONGO_URI]):
    logger.error("❌ خطأ فادح: واحد أو أكثر من متغيرات البيئة الأساسية غير موجود.")
    # exit()

# ==============================================================================
# 4. إعدادات قاعدة البيانات (MongoDB)
# ==============================================================================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ تم الاتصال بقاعدة البيانات بنجاح.")
except Exception as e:
    logger.error(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    client_db = None # Set to None to handle DB errors gracefully

# ==============================================================================
# 5. إعداد تطبيق Flask وعميل OpenAI
# ==============================================================================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
application = None # سيتم تهيئته بشكل غير متزامن

# ==============================================================================
# 6. متغيرات عالمية وأقفال
# ==============================================================================
pending_messages = {}
timers = {}
thread_locks = {}
client_processing_locks = {}

# ==============================================================================
# 7. دوال إدارة الجلسات (مشتركة)
# ==============================================================================
def get_session(user_id):
    user_id_str = str(user_id)
    if not client_db: return None # Handle case where DB is not connected
    session = sessions_collection.find_one({"_id": user_id_str})
    if not session:
        session = {
            "_id": user_id_str, "history": [], "thread_id": None, "message_count": 0,
            "name": "", "last_message_time": datetime.utcnow().isoformat(),
            "follow_up_sent": 0, "follow_up_status": "none", "last_follow_up_time": None,
            "payment_status": "pending"
        }
    session.setdefault("last_message_time", datetime.utcnow().isoformat())
    session.setdefault("follow_up_sent", 0)
    session.setdefault("follow_up_status", "none")
    return session

def save_session(user_id, session_data):
    user_id_str = str(user_id)
    if not client_db: return # Handle case where DB is not connected
    session_data["_id"] = user_id_str
    sessions_collection.replace_one({"_id": user_id_str}, session_data, upsert=True)
    logger.info(f"💾 تم حفظ بيانات الجلسة للمستخدم {user_id_str}.")

# ==============================================================================
# 8. دوال مشتركة (إرسال، تحويل صوت، مساعد)
# ==============================================================================
def send_whatsapp_message(phone, message):
    # ... (الكود الخاص بك بدون تغيير) ...
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        logger.info(f"📤 [WhatsApp] تم إرسال رسالة للعميل {phone}، الحالة: {response.status_code}")
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [WhatsApp] خطأ أثناء إرسال الرسالة عبر ZAPI: {e}")

async def send_telegram_message(context, chat_id, message):
    # ... (الكود الخاص بك بدون تغيير) ...
    try:
        await context.bot.send_message(chat_id=chat_id, text=message)
        logger.info(f"📤 [Telegram] تم إرسال رسالة للعميل {chat_id}.")
    except Exception as e:
        logger.error(f"❌ [Telegram] خطأ أثناء إرسال الرسالة: {e}")

def transcribe_audio(audio_url, file_format="ogg"):
    # ... (الكود الخاص بك بدون تغيير) ...
    logger.info(f"🎙️ محاولة تحميل وتحويل الصوت من: {audio_url}")
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

def ask_assistant(content, sender_id, name=""):
    # ... (الكود الخاص بك بدون تغيير) ...
    session = get_session(sender_id)
    if name and not session.get("name"):
        session["name"] = name
    
    if not session.get("thread_id"):
        try:
            thread = client.beta.threads.create()
            session["thread_id"] = thread.id
        except Exception as e:
            logger.error(f"❌ فشل إنشاء Thread جديد: {e}")
            return "⚠ مشكلة مؤقتة في إنشاء المحادثة، حاول مرة أخرى."

    if not isinstance(content, list):
        content = [{"type": "text", "text": content}]

    thread_id_str = str(session["thread_id"])
    if thread_id_str not in thread_locks:
        thread_locks[thread_id_str] = threading.Lock()

    with thread_locks[thread_id_str]:
        try:
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
            else:
                logger.error(f"❌ الـ Run فشل أو توقف: {run.status}")
                return "⚠ حدث خطأ أثناء معالجة طلبك، حاول مرة أخرى."
        except Exception as e:
            logger.error(f"❌ استثناء أثناء التفاعل مع المساعد: {e}")
            return "⚠ مشكلة مؤقتة، حاول مرة أخرى."

# ==============================================================================
# 9. منطق WhatsApp (Flask Webhook)
# ==============================================================================
# ... (الكود الخاص بك بدون تغيير) ...
def process_whatsapp_messages(sender, name):
    sender_str = str(sender)
    with client_processing_locks.setdefault(sender_str, threading.Lock()):
        time.sleep(8)
        if not pending_messages.get(sender_str):
            timers.pop(sender_str, None)
            return

        combined_text = "\n".join(pending_messages[sender_str])
        reply = ask_assistant(combined_text, sender_str, name)

        typing_delay = max(1, min(len(reply) / 5.0, 8))
        time.sleep(typing_delay)

        send_whatsapp_message(sender_str, reply)
        
        pending_messages[sender_str] = []
        timers.pop(sender_str, None)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    sender = data.get("phone")
    if not sender: return jsonify({"status": "no sender"}), 400

    session = get_session(sender)
    session["last_message_time"] = datetime.utcnow().isoformat()
    session["follow_up_sent"] = 0
    session["follow_up_status"] = "responded"
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
        if sender_str not in pending_messages: pending_messages[sender_str] = []
        pending_messages[sender_str].append(msg)
        if sender_str not in timers:
            timers[sender_str] = threading.Thread(target=process_whatsapp_messages, args=(sender_str, name))
            timers[sender_str].start()
            
    return jsonify({"status": "received"}), 200

# ==============================================================================
# 10. منطق Telegram (Handlers)
# ==============================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"مرحباً {user.first_name}! أنا هنا لمساعدتك.")

async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name
    
    session = get_session(chat_id)
    session["last_message_time"] = datetime.utcnow().isoformat()
    session["follow_up_sent"] = 0
    session["follow_up_status"] = "responded"
    save_session(chat_id, session)

    await context.bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)
    
    reply = ""
    content_for_assistant = ""

    if update.message.text:
        content_for_assistant = update.message.text
    elif update.message.voice:
        voice_file = await update.message.voice.get_file()
        transcribed_text = transcribe_audio(voice_file.file_path)
        if transcribed_text:
            content_for_assistant = f"رسالة صوتية من العميل: {transcribed_text}"
        else:
            reply = "عذراً، لم أتمكن من فهم رسالتك الصوتية."
    elif update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        caption = update.message.caption or ""
        content_list = [{"type": "image_url", "image_url": {"url": photo_file.file_path}}]
        if caption: content_list.append({"type": "text", "text": f"تعليق على الصورة: {caption}"})
        content_for_assistant = content_list

    if content_for_assistant and not reply:
        reply = ask_assistant(content_for_assistant, chat_id, user_name)

    if reply:
        await send_telegram_message(context, chat_id, reply)

# *** معالجات جديدة لرسائل الأعمال ***
async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    chat_id = message.chat.id
    user_name = message.chat.first_name or "Business Client"
    logger.info(f"🏢 رسالة أعمال من {user_name} ({chat_id}): {message.text}")

    session = get_session(chat_id)
    session["last_message_time"] = datetime.utcnow().isoformat()
    save_session(chat_id, session)

    await context.bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)
    reply = ask_assistant(message.text, chat_id, user_name)
    await send_telegram_message(context, chat_id, reply)

async def handle_edited_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.edited_business_message
    # نعيد استخدام نفس منطق المعالج الرئيسي
    mock_update = Update(update.update_id, business_message=message)
    await handle_business_message(mock_update, context)

async def handle_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    connection = update.business_connection
    logger.info(f"🤝 تحديث اتصال أعمال: ID={connection.id}, UserID={connection.user_chat_id}, Enabled={connection.is_enabled}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)

# ==============================================================================
# 11. مسارات Flask و Webhook
# ==============================================================================
@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def telegram_webhook_handler():
    if not application:
        logger.error("تطبيق تيليجرام غير مهيأ.")
        return jsonify({"status": "error"}), 500
    update_data = request.get_json()
    await application.process_update(
        Update.de_json(update_data, application.bot)
    )
    return jsonify({"status": "ok"})

@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر يعمل (واتساب و تيليجرام)."

# ==============================================================================
# 12. نظام المتابعة التلقائية (Scheduler)
# ==============================================================================
def check_for_inactive_users():
    pass 

scheduler = BackgroundScheduler()
# scheduler.add_job(check_for_inactive_users, 'interval', minutes=5)
scheduler.start()
logger.info("⏰ تم بدء الجدولة بنجاح.")

# ==============================================================================
# 13. دالة التشغيل الرئيسية (Main)
# ==============================================================================
async def main():
    global application
    logger.info("🔧 تهيئة تطبيق تيليجرام...")
    
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # ربط الـ Handlers بالتطبيق
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_telegram_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_telegram_message))
    
    # *** ربط معالجات رسائل الأعمال الجديدة ***
    application.add_handler(BusinessConnectionHandler(handle_business_connection))
    application.add_handler(BusinessMessageHandler(handle_business_message))
    application.add_handler(EditedBusinessMessageHandler(handle_edited_business_message))
    
    application.add_error_handler(error_handler)

    # إعداد الـ Webhook
    if WEBHOOK_URL:
        logger.info(f"🔧 إعداد الـ Webhook على: {WEBHOOK_URL}")
        await application.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES, # *** مهم: يسمح بكل أنواع التحديثات ***
            drop_pending_updates=True
        )
    else:
        logger.warning("⚠️ لم يتم إعداد الـ Webhook بسبب نقص متغيرات البيئة.")

    await application.initialize()
    
    # تشغيل Flask في خيط منفصل
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, use_reloader=False, debug=False)
    )
    flask_thread.start()
    logger.info(f"🚀 سيرفر Flask بدأ العمل على المنفذ {PORT}")

    # إبقاء الدالة الرئيسية تعمل
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("...إيقاف التشغيل")
