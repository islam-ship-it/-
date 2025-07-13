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
from asgiref.wsgi import WsgiToAsgi
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# Telegram imports
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# ==============================================================================
# إعداد نظام التسجيل (Logging)
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# تحميل متغيرات البيئة
# ==============================================================================
load_dotenv()

# ==============================================================================
# إعدادات البيئة
# ==============================================================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
FOLLOW_UP_INTERVAL_MINUTES = int(os.getenv("FOLLOW_UP_INTERVAL_MINUTES", 1440))
MAX_FOLLOW_UPS = int(os.getenv("MAX_FOLLOW_UPS", 3))

# ==============================================================================
# التحقق من المتغيرات الأساسية
# ==============================================================================
if not all([OPENAI_API_KEY, ASSISTANT_ID_PREMIUM, TELEGRAM_BOT_TOKEN, MONGO_URI]):
    logger.critical("❌ خطأ فادح: واحد أو أكثر من متغيرات البيئة الأساسية غير موجود.")
    exit()

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

# ==============================================================================
# دوال إدارة الجلسات (مشتركة)
# ==============================================================================
def get_session(user_id):
    user_id_str = str(user_id)
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
    session_data["_id"] = user_id_str
    sessions_collection.replace_one({"_id": user_id_str}, session_data, upsert=True)
    logger.info(f"💾 تم حفظ بيانات الجلسة للمستخدم {user_id_str}.")

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
# دوال مشتركة (تحويل الصوت، التفاعل مع المساعد)
# ==============================================================================
def transcribe_audio(audio_url, file_format="ogg"):
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
    session = get_session(sender_id)
    if name and not session.get("name"):
        session["name"] = name
    
    if not session.get("thread_id"):
        try:
            thread = client.beta.threads.create()
            session["thread_id"] = thread.id
        except Exception as e:
            logger.error(f"❌ فشل إنشاء Thread جديد للمستخدم {sender_id}: {e}")
            return "⚠ مشكلة مؤقتة في إنشاء المحادثة، حاول مرة أخرى."

    if isinstance(content, str):
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
                logger.error(f"❌ الـ Run فشل أو توقف للمستخدم {sender_id}: {run.status}")
                return "⚠ حدث خطأ أثناء معالجة طلبك، حاول مرة أخرى."
        except Exception as e:
            logger.error(f"❌ استثناء أثناء التفاعل مع المساعد للمستخدم {sender_id}: {e}")
            return "⚠ مشكلة مؤقتة، حاول مرة أخرى."

# ==============================================================================
# منطق WhatsApp (Flask Webhook)
# ==============================================================================
def process_whatsapp_messages(sender, name):
    sender_str = str(sender)
    with client_processing_locks.setdefault(sender_str, threading.Lock()):
        time.sleep(8)
        if not pending_messages.get(sender_str):
            timers.pop(sender_str, None)
            return

        combined_text = "\n".join(pending_messages[sender_str])
        logger.info(f"🔄 [WhatsApp] معالجة الرسائل المجمعة للمستخدم {sender_str}: {combined_text}")
        reply = ask_assistant(combined_text, sender_str, name)

        typing_delay = max(1, min(len(reply) / 5.0, 8))
        time.sleep(typing_delay)

        send_whatsapp_message(sender_str, reply)
        
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
# منطق Telegram (Webhook) - معدل لدعم Business
# ==============================================================================
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

async def start_command(update, context):
    user = update.effective_user
    await update.message.reply_text(f"مرحباً {user.first_name}! أنا هنا لمساعدتك.")

async def handle_any_message(update, context):
    # نحدد ما إذا كانت رسالة عادية أم رسالة أعمال
    message = update.message or update.business_message

    if not message:
        logger.info("التحديث لا يحتوي على رسالة يمكن معالجتها، سيتم تجاهله.")
        return

    chat_id = message.chat_id
    user_name = message.from_user.first_name
    logger.info(f"📥 [Telegram] رسالة جديدة من: {chat_id} - الاسم: {user_name}")
    
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)
    except Exception as e:
        logger.error(f"❌ [Telegram] فشل إرسال chat action للمستخدم {chat_id}: {e}")
    
    session = get_session(chat_id)
    session["last_message_time"] = datetime.utcnow().isoformat()
    session["follow_up_sent"] = 0
    session["follow_up_status"] = "responded"
    save_session(chat_id, session)
    
    reply = ""
    content_for_assistant = ""

    if message.text:
        content_for_assistant = message.text
    elif message.voice:
        voice_file = await message.voice.get_file()
        transcribed_text = transcribe_audio(voice_file.file_path)
        if transcribed_text:
            content_for_assistant = f"رسالة صوتية من العميل: {transcribed_text}"
        else:
            reply = "عذراً، لم أتمكن من فهم رسالتك الصوتية."
    elif message.photo:
        photo_file = await message.photo[-1].get_file()
        caption = message.caption or ""
        content_list = [{"type": "image_url", "image_url": {"url": photo_file.file_path}}]
        if caption: content_list.append({"type": "text", "text": f"تعليق على الصورة: {caption}"})
        content_for_assistant = content_list

    if content_for_assistant and not reply:
        reply = ask_assistant(content_for_assistant, chat_id, user_name)

    if reply:
        await context.bot.send_message(chat_id=chat_id, text=reply)

# --- ربط الـ Handlers ---
telegram_app.add_handler(CommandHandler("start", start_command))
# معالج للرسائل العادية (عندما تراسل البوت مباشرة)
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_any_message))
telegram_app.add_handler(MessageHandler(filters.VOICE, handle_any_message))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_any_message))
# معالج لرسائل الأعمال (عندما يراسل العملاء حسابك الشخصي)
telegram_app.add_handler(MessageHandler(filters.BUSINESS_MESSAGE, handle_any_message))

@flask_app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def telegram_webhook_handler():
    update_data = request.get_json()
    logger.info("📥 [Telegram Webhook] بيانات مستلمة.")
    await telegram_app.process_update(
        telegram.Update.de_json(update_data, telegram_app.bot)
    )
    return jsonify({"status": "ok"})

@flask_app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر يعمل (واتساب و تيليجرام)."

async def setup_telegram():
    render_hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME')
    if render_hostname:
        logger.info("🔧 جاري تهيئة تطبيق تيليجرام وإعداد الـ Webhook...")
        await telegram_app.initialize()
        webhook_url = f"https://{render_hostname}/{TELEGRAM_BOT_TOKEN}"
        await telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=telegram.Update.ALL_TYPES )
        logger.info("✅ [Telegram] تم تهيئة التطبيق وإعداد الـ Webhook بنجاح.")
    else:
        logger.warning("⚠️ لم يتم العثور على RENDER_EXTERNAL_HOSTNAME. تخطي إعداد الـ Webhook.")

# نقوم بتشغيل دالة الإعداد عند بدء التشغيل
try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(setup_telegram())
    else:
        asyncio.run(setup_telegram())
except Exception as e:
    logger.critical(f"❌ فشل إعداد تيليجرام أثناء بدء التشغيل: {e}")

# ==============================================================================
# نظام المتابعة التلقائية (Scheduler)
# ==============================================================================
def check_for_inactive_users():
    pass 

scheduler = BackgroundScheduler()
scheduler.start()
logger.info("⏰ تم بدء الجدولة بنجاح.")

# ==============================================================================
# تشغيل التطبيق
# ==============================================================================
if __name__ == "__main__":
    logger.info("🚀 جاري بدء تشغيل السيرفر للاختبار المحلي (لا تستخدم هذا في الإنتاج)...")
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=True)
