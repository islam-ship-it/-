import os
import time
import json
import requests
import asyncio
import logging
from flask import Flask, request, jsonify
from asgiref.wsgi import WsgiToAsgi
from openai import OpenAI
from pymongo import MongoClient
from dotenv import load_dotenv

# Telegram imports
import telegram
from telegram.ext import Application, MessageHandler, filters

# إعداد تسجيل الأحداث
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# تحميل متغيرات البيئة
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")

if not all([OPENAI_API_KEY, ASSISTANT_ID_PREMIUM, TELEGRAM_BOT_TOKEN, MONGO_URI]):
    logger.critical("❌ خطأ: متغيرات البيئة ناقصة.")
    exit()

# إعداد الاتصال بقاعدة البيانات
client_db = MongoClient(MONGO_URI)
db = client_db["multi_platform_bot"]
sessions_collection = db["sessions"]

# إعداد Flask و ASGI
flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)

# إعداد OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# إعداد تطبيق تيليجرام
telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# إدارة الجلسات
def get_session(user_id):
    uid = str(user_id)
    session = sessions_collection.find_one({"_id": uid})
    if not session:
        session = {"_id": uid, "thread_id": None, "history": []}
    return session

def save_session(user_id, session):
    sessions_collection.replace_one({"_id": str(user_id)}, session, upsert=True)

# إرسال الرد من حساب تجاري (Telegram Business)
def send_business_reply(text, business_connection_id):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "business_connection_id": business_connection_id,
            "text": text
        }
        headers = {"Content-Type": "application/json"}
        res = requests.post(url, json=payload, headers=headers)
        logger.info(f"📤 تم إرسال رد من الحساب التجاري. {res.status_code} - {res.text}")
    except Exception as e:
        logger.error(f"❌ فشل إرسال من الحساب التجاري: {e}")

# المحادثة مع مساعد OpenAI
def ask_assistant(content, sender_id):
    session = get_session(sender_id)
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id

    client.beta.threads.messages.create(
        thread_id=session["thread_id"],
        role="user",
        content=[{"type": "text", "text": content}]
    )

    run = client.beta.threads.runs.create(
        thread_id=session["thread_id"],
        assistant_id=ASSISTANT_ID_PREMIUM
    )

    while run.status in ["queued", "in_progress"]:
        time.sleep(1)
        run = client.beta.threads.runs.retrieve(
            thread_id=session["thread_id"],
            run_id=run.id
        )

    if run.status == "completed":
        messages = client.beta.threads.messages.list(thread_id=session["thread_id"])
        reply = messages.data[0].content[0].text.value.strip()
        save_session(sender_id, session)
        return reply

    return "⚠ حدث خطأ أثناء معالجة رد المساعد."

# التعامل مع الرسائل الواردة
async def handle_telegram_message(update, context):
    msg = update.business_message or update.message
    if not msg:
        return

    text = msg.text or ""
    chat_id = msg.chat.id
    business_connection_id = getattr(update.business_message, 'business_connection_id', None)

    logger.info(f"📩 تم استلام رسالة: {text}")
    reply = ask_assistant(text, chat_id)

    if business_connection_id:
        send_business_reply(reply, business_connection_id)
    else:
        await context.bot.send_message(chat_id=chat_id, text=reply)

# إعداد Webhook
@flask_app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def telegram_webhook_handler():
    update_data = request.get_json()
    await telegram_app.process_update(
        telegram.Update.de_json(update_data, telegram_app.bot)
    )
    return jsonify({"status": "ok"})

@flask_app.route("/")
def home():
    return "✅ البوت يعمل."

telegram_app.add_handler(MessageHandler(filters.ALL, handle_telegram_message))

# إعداد Webhook عند التشغيل
async def setup():
    if RENDER_EXTERNAL_HOSTNAME:
        await telegram_app.initialize()
        await telegram_app.bot.set_webhook(
            url=f"https://{RENDER_EXTERNAL_HOSTNAME}/{TELEGRAM_BOT_TOKEN}"
        )

# بدء التطبيق
try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(setup())
    else:
        asyncio.run(setup())
except Exception as e:
    logger.critical(f"❌ فشل إعداد Webhook: {e}")

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)

