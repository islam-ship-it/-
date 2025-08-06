import os
import time
import json
import requests
import threading
import asyncio
import logging
import random
# لا حاجة لمكتبة queue بعد الآن
# import queue

from flask import Flask, request, jsonify
from asgiref.wsgi import WsgiToAsgi
from openai import OpenAI
from pymongo import MongoClient, ReturnDocument
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# ... (باقي إعداداتك كما هي) ...
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)
load_dotenv()
# ... (تحميل كل متغيرات البيئة) ...
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# ==============================================================================
# إعدادات قاعدة البيانات (MongoDB)
# ==============================================================================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    # ##### تعديل #####: إنشاء collection جديد ليكون بمثابة طابور
    outgoing_collection = db["outgoing_whatsapp"]
    logger.info("✅ تم الاتصال بقاعدة البيانات وإعداد المجموعات بنجاح.")
except Exception as e:
    logger.critical(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    exit()

# ... (إعداد Flask و OpenAI كما هو) ...
flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
client = OpenAI(api_key=OPENAI_API_KEY)

# ==============================================================================
# متغيرات عالمية (تم تبسيطها)
# ==============================================================================
pending_messages = {}
timers = {}
thread_locks = {}
client_processing_locks = {}
# لم نعد بحاجة إلى whatsapp_message_queue

# ... (دوال get_session و save_session كما هي) ...
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

# ==============================================================================
# ##### تعديل جذري #####: نظام إرسال رسائل واتساب عبر قاعدة البيانات
# ==============================================================================
def process_db_queue():
    """
    هذه الدالة يتم استدعاؤها بشكل دوري بواسطة APScheduler.
    تبحث عن رسالة واحدة في حالة 'pending' في قاعدة البيانات، ترسلها، ثم تحدث حالتها.
    """
    try:
        # ابحث عن رسالة واحدة معلقة وقم بتحديث حالتها إلى 'processing' في خطوة واحدة (atomic)
        # هذا يمنع العمال الآخرين من أخذ نفس الرسالة
        message_to_send = outgoing_collection.find_one_and_update(
            {"status": "pending"},
            {"$set": {"status": "processing", "processed_at": datetime.utcnow()}},
            sort=[("created_at", 1)],  # خذ أقدم رسالة أولاً
            return_document=ReturnDocument.AFTER
        )

        if message_to_send:
            phone = message_to_send["phone"]
            message_text = message_to_send["message"]
            message_id = message_to_send["_id"]
            
            logger.info(f"📤 [DB Queue] تم سحب رسالة ({message_id}) من قاعدة البيانات للرقم {phone}.")

            url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
            headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
            payload = {"phone": phone, "message": message_text}

            try:
                response = requests.post(url, headers=headers, json=payload, timeout=20)
                logger.info(f"✅ [ZAPI] تم إرسال الرسالة ({message_id}) بنجاح، الحالة: {response.status_code}")
                response.raise_for_status()
                # تحديث الحالة النهائية إلى 'sent'
                outgoing_collection.update_one({"_id": message_id}, {"$set": {"status": "sent", "sent_at": datetime.utcnow()}})
            except requests.exceptions.RequestException as e:
                logger.error(f"❌ [ZAPI] فشل إرسال الرسالة ({message_id}): {e}")
                # إعادة الحالة إلى 'pending' لمحاولة أخرى لاحقًا
                outgoing_collection.update_one({"_id": message_id}, {"$set": {"status": "pending", "error_count": message_to_send.get("error_count", 0) + 1}})
        else:
            # هذا طبيعي، يعني أن الطابور (قاعدة البيانات) فارغ
            logger.info("...[DB Queue] لا توجد رسائل معلقة لإرسالها...")
            
    except Exception as e:
        logger.error(f"❌ [DB Queue Processor] حدث خطأ جسيم: {e}")

def send_whatsapp_message(phone, message):
    """
    هذه الدالة الآن تقوم بإدراج الرسالة في قاعدة البيانات (collection: outgoing_whatsapp).
    """
    logger.info(f"📥 [DB Queue] إضافة رسالة إلى قاعدة البيانات للرقم: {phone}")
    outgoing_collection.insert_one({
        "phone": str(phone),
        "message": message,
        "status": "pending", # الحالات الممكنة: pending, processing, sent, failed
        "created_at": datetime.utcnow(),
        "error_count": 0
    })

# ... (باقي الدوال: send_telegram_message, transcribe_audio, ask_assistant, process_whatsapp_messages, webhook, ومنطق تيليجرام بالكامل يبقى كما هو) ...
# ... (لا تغيير في هذه الدوال) ...
async def send_telegram_message(context, chat_id, message, business_connection_id=None):
    try:
        if business_connection_id:
            await context.bot.send_message(chat_id=chat_id, text=message, business_connection_id=business_connection_id)
        else:
            await context.bot.send_message(chat_id=chat_id, text=message)
        logger.info(f"📤 Sent to Telegram user {chat_id}")
    except Exception as e:
        logger.error(f"❌ Telegram send error: {e}")

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

def process_whatsapp_messages(sender, name):
    sender_str = str(sender)
    with client_processing_locks.setdefault(sender_str, threading.Lock()):
        time.sleep(15)
        if not pending_messages.get(sender_str):
            timers.pop(sender_str, None)
            return

        combined_text = "\n".join(pending_messages[sender_str])
        reply = ask_assistant(combined_text, sender_str, name)
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

telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

async def start_command(update, context):
    await update.message.reply_text(f"أهلاً {update.effective_user.first_name}!")

async def handle_telegram_message(update, context):
    message = update.message or update.business_message
    if not message: return
    chat_id = message.chat.id
    user_name = message.from_user.first_name
    business_id = getattr(update.business_message, "business_connection_id", None) if hasattr(update, "business_message") and update.business_message else None
    logger.info(f"========== تحديث جديد تليجرام | Chat ID: {chat_id}, Name: {user_name}, Business ID: {business_id} ==========")
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING, business_connection_id=business_id)
    except Exception as e:
        logger.warning(f"⚠️ لم يتمكن من إرسال chat action: {e}")
    session = get_session(chat_id)
    session["last_message_time"] = datetime.utcnow().isoformat()
    save_session(chat_id, session)
    reply_text, content_for_assistant = "", ""
    if message.text:
        content_for_assistant = message.text
    elif message.voice:
        voice_file = await message.voice.get_file()
        transcribed_text = transcribe_audio(voice_file.file_path)
        content_for_assistant = f"رسالة صوتية من العميل: {transcribed_text}" if transcribed_text else ""
        if not content_for_assistant: reply_text = "عذراً، لم أتمكن من فهم رسالتك الصوتية."
    elif message.photo:
        photo_file = await message.photo[-1].get_file()
        caption = message.caption or ""
        content_list = [{"type": "image_url", "image_url": {"url": photo_file.file_path}}]
        if caption: content_list.append({"type": "text", "text": f"تعليق على الصورة: {caption}"})
        content_for_assistant = content_list
    if content_for_assistant and not reply_text:
        reply_text = ask_assistant(content_for_assistant, chat_id, user_name)
    if reply_text:
        await send_telegram_message(context, chat_id, reply_text, business_connection_id=business_id)

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
        webhook_url = f"https://{host}/{TELEGRAM_BOT_TOKEN}"
        await telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=telegram.Update.ALL_TYPES  )
        logger.info(f"✅ تم إعداد Telegram Webhook على: {webhook_url}")

# ==============================================================================
# ##### تعديل #####: إعداد وتشغيل APScheduler لمعالجة الطابور من قاعدة البيانات
# ==============================================================================
scheduler = BackgroundScheduler(timezone="UTC")
# اجعل المهمة تعمل كل 15 ثانية مع تفاوت عشوائي
scheduler.add_job(
    func=process_db_queue,
    trigger="interval",
    seconds=15,
    jitter=5,
    id="db_queue_processor",
    name="Process the MongoDB message queue",
    replace_existing=True
)
scheduler.start()
logger.info("🚀 تم تشغيل مجدول المهام (APScheduler) لمعالجة طابور الرسائل من قاعدة البيانات.")

# ... (تشغيل التطبيق كما هو) ...
if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(setup_telegram())
        else:
            asyncio.run(setup_telegram())
    except Exception as e:
        logger.critical(f"❌ Telegram setup failed: {e}")
        
    port = int(os.environ.get("PORT", 5000))
    # لا تقم بتشغيل وضع التصحيح في الإنتاج
    # flask_app.run(host="0.0.0.0", port=port, debug=False)
    # Gunicorn هو الذي سيقوم بتشغيل التطبيق، لذلك هذا السطر ليس ضروريًا عند النشر
    # ولكن اتركه إذا كنت تختبر محليًا
