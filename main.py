import os
import time
import json
import requests
import threading
import asyncio
import logging
from flask import Flask, request, jsonify
from asgiref.wsgi import WsgiToAsgi
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

# Load .env
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# DB setup
client_db = MongoClient(MONGO_URI)
db = client_db["multi_platform_bot"]
sessions_collection = db["sessions"]

# Flask + ASGI
flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
client = OpenAI(api_key=OPENAI_API_KEY)

thread_locks = {}

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

async def send_telegram_message(context, chat_id, message, business_connection_id=None):
    try:
        if business_connection_id:
            await context.bot.send_message(chat_id=chat_id, text=message, business_connection_id=business_connection_id)
        else:
            await context.bot.send_message(chat_id=chat_id, text=message)
        logger.info(f"📤 Sent to Telegram user {chat_id}")
    except Exception as e:
        logger.error(f"❌ Telegram send error: {e}")

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
        return "⚠️ حصل خطأ، جرب تاني."

telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

async def start_command(update, context):
    await update.message.reply_text(f"أهلاً {update.effective_user.first_name}!")

async def handle_telegram_message(update, context):
    message = update.message or update.business_message
    if not message:
        return
    chat_id = message.chat.id
    user_name = message.from_user.first_name

    # الحصول على business_connection_id بشكل صحيح
    business_id = None
    if hasattr(update, "business_message") and update.business_message:
        business_id = getattr(update.business_message, "business_connection_id", None)

    logger.info("========== تحديث جديد ==========")
    logger.info(f"🔍 chat_id: {chat_id}, name: {user_name}")
    logger.info(f"🔗 business_connection_id: {business_id}")
    logger.info("📦 full update:\n" + json.dumps(update.to_dict(), indent=2, ensure_ascii=False))

    await context.bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)

    session = get_session(chat_id)
    session["last_message_time"] = datetime.utcnow().isoformat()
    session["follow_up_sent"] = 0
    session["follow_up_status"] = "responded"
    save_session(chat_id, session)

    if message.text:
        reply = ask_assistant(message.text, chat_id, user_name)
        await send_telegram_message(context, chat_id, reply, business_connection_id=business_id)

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
        await telegram_app.bot.set_webhook(url=f"https://{host}/{TELEGRAM_BOT_TOKEN}", allowed_updates=telegram.Update.ALL_TYPES)

try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(setup_telegram())
    else:
        asyncio.run(setup_telegram())
except Exception as e:
    logger.critical(f"❌ Telegram setup failed: {e}")

scheduler = BackgroundScheduler()
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=True)
