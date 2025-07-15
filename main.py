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

# إعداد نظام التسجيل (Logging)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

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

if not all([OPENAI_API_KEY, ASSISTANT_ID_PREMIUM, TELEGRAM_BOT_TOKEN, MONGO_URI]):
    logger.critical("❌ خطأ فادح: واحد أو أكثر من متغيرات البيئة الأساسية غير موجود.")
    exit()

try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ تم الاتصال بقاعدة البيانات بنجاح.")
except Exception as e:
    logger.critical(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    exit()

flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
client = OpenAI(api_key=OPENAI_API_KEY)

pending_messages = {}
timers = {}
thread_locks = {}
client_processing_locks = {}

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

async def send_telegram_message(context, chat_id, message, business_connection_id=None):
    try:
        if business_connection_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                business_connection_id=business_connection_id
            )
        else:
            await context.bot.send_message(chat_id=chat_id, text=message)
        logger.info(f"📤 [Telegram] تم إرسال رسالة للعميل {chat_id}.")
    except Exception as e:
        logger.error(f"❌ [Telegram] خطأ أثناء إرسال الرسالة: {e}")

telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

async def start_command(update, context):
    user = update.effective_user
    await update.message.reply_text(f"مرحباً {user.first_name}! أنا هنا لمساعدتك.")

async def handle_telegram_message(update, context):
    chat = update.effective_chat
    user = update.effective_user
    message_to_process = update.message or update.business_message

    if chat and user:
        logger.info(f"📥 [Telegram Update] تحديث جديد من: {chat.id} - الاسم: {user.first_name}")

    if not message_to_process:
        logger.info("✅ التجاهل: التحديث لا يحتوي على رسالة جديدة يمكن معالجتها.")
        return

    chat_id = message_to_process.chat.id
    user_name = message_to_process.from_user.first_name
    business_id = getattr(update, "business_connection_id", None)

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

    if message_to_process.text:
        logger.info(f"💬 نوع الرسالة: نص. المحتوى: '{message_to_process.text}'")
        content_for_assistant = message_to_process.text

    if content_for_assistant and not reply:
        reply = ask_assistant(content_for_assistant, chat_id, user_name)

    if reply:
        await send_telegram_message(context, chat_id, reply, business_connection_id=business_id)

all_messages_handler = MessageHandler(filters.ALL, handle_telegram_message)
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(all_messages_handler)

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
        await telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=telegram.Update.ALL_TYPES)
        logger.info("✅ [Telegram] تم تهيئة التطبيق وإعداد الـ Webhook بنجاح.")
    else:
        logger.warning("⚠ لم يتم العثور على RENDER_EXTERNAL_HOSTNAME. تخطي إعداد الـ Webhook.")

try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(setup_telegram())
    else:
        asyncio.run(setup_telegram())
except Exception as e:
    logger.critical(f"❌ فشل إعداد تيليجرام أثناء بدء التشغيل: {e}")

scheduler = BackgroundScheduler()
scheduler.start()
logger.info("⏰ تم بدء الجدولة بنجاح.")

if __name__ == "__main__":
    logger.info("🚀 جاري بدء تشغيل السيرفر للاختبار المحلي...")
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=True)
