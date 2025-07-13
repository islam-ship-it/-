import os
import time
import json
import requests
import threading
import traceback
import random
import asyncio
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# Telegram imports
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

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
# إعدادات قاعدة البيانات (MongoDB)
# ==============================================================================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    print("✅ تم الاتصال بقاعدة البيانات بنجاح.", flush=True)
except Exception as e:
    print(f"❌ فشل الاتصال بقاعدة البيانات: {e}", flush=True)
    exit()

# ==============================================================================
# إعداد تطبيق Flask وعميل OpenAI
# ==============================================================================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

# ==============================================================================
# متغيرات عالمية (تبقى كما هي)
# ==============================================================================
pending_messages = {}
timers = {}
thread_locks = {}
client_processing_locks = {}

# ==============================================================================
# دوال إدارة الجلسات (مشتركة - تبقى كما هي)
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
    print(f"💾 تم حفظ بيانات الجلسة للمستخدم {user_id_str}.", flush=True)

# ==============================================================================
# دوال إرسال الرسائل (تبقى كما هي)
# ==============================================================================
def send_whatsapp_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        print(f"📤 [WhatsApp] تم إرسال رسالة للعميل {phone}، الحالة: {response.status_code}", flush=True)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"❌ [WhatsApp] خطأ أثناء إرسال الرسالة عبر ZAPI: {e}", flush=True)

async def send_telegram_message(context, chat_id, message):
    try:
        await context.bot.send_message(chat_id=chat_id, text=message)
        print(f"📤 [Telegram] تم إرسال رسالة للعميل {chat_id}.", flush=True)
    except Exception as e:
        print(f"❌ [Telegram] خطأ أثناء إرسال الرسالة: {e}", flush=True)

# ==============================================================================
# دوال مشتركة (تحويل الصوت، التفاعل مع المساعد - تبقى كما هي)
# ==============================================================================
def transcribe_audio(audio_url, file_format="ogg"):
    print(f"🎙️ محاولة تحميل وتحويل الصوت من: {audio_url}", flush=True)
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
        print(f"❌ خطأ أثناء تحويل الصوت إلى نص: {e}", flush=True)
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
            print(f"❌ فشل إنشاء Thread جديد: {e}", flush=True)
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
                print(f"❌ الـ Run فشل أو توقف: {run.status}", flush=True)
                return "⚠ حدث خطأ أثناء معالجة طلبك، حاول مرة أخرى."
        except Exception as e:
            print(f"❌ استثناء أثناء التفاعل مع المساعد: {e}", flush=True)
            return "⚠ مشكلة مؤقتة، حاول مرة أخرى."

# ==============================================================================
# منطق WhatsApp (Flask Webhook - يبقى كما هو)
# ==============================================================================
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
# --- التعديلات تبدأ هنا ---
# ==============================================================================

# 1. إعداد تطبيق تيليجرام بشكل عام ليتم استخدامه لاحقاً
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# 2. تعريف معالجات رسائل تيليجرام (Handlers - تبقى كما هي)
async def start_command(update, context):
    user = update.effective_user
    await update.message.reply_text(f"مرحباً {user.first_name}! أنا هنا لمساعدتك.")

async def handle_telegram_message(update, context):
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

# 3. ربط الـ Handlers بالتطبيق
application.add_handler(CommandHandler("start", start_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
application.add_handler(MessageHandler(filters.VOICE, handle_telegram_message))
application.add_handler(MessageHandler(filters.PHOTO, handle_telegram_message))

# 4. إضافة مسار Webhook جديد لتليجرام
@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def telegram_webhook_handler():
    """
    يستقبل التحديثات من تيليجرام ويمررها للمعالج.
    """
    update_data = request.get_json()
    print(f"📥 [Telegram Webhook] بيانات مستلمة.", flush=True)
    await application.process_update(
        telegram.Update.de_json(update_data, application.bot)
    )
    return jsonify({"status": "ok"})

# 5. تعديل المسار الرئيسي ليعكس وجود المنصتين
@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر يعمل (واتساب و تيليجرام)."

# ==============================================================================
# نظام المتابعة التلقائية (Scheduler - يبقى كما هو ومعطل)
# ==============================================================================
def check_for_inactive_users():
    pass 

# ==============================================================================
# تشغيل التطبيق (الجزء المعدل)
# ==============================================================================
if __name__ == "__main__":
    if not all([OPENAI_API_KEY, ASSISTANT_ID_PREMIUM, TELEGRAM_BOT_TOKEN, MONGO_URI]):
        print("❌ خطأ: واحد أو أكثر من متغيرات البيئة الأساسية غير موجود. يرجى مراجعة الإعدادات.")
        exit()

    # إعداد الـ Webhook عند بدء التشغيل (فقط إذا كان يعمل على Render)
    render_hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME')
    if render_hostname:
        print("🔧 جاري إعداد Webhook تيليجرام...", flush=True)
        webhook_url = f"https://{render_hostname}/{TELEGRAM_BOT_TOKEN}"
        
        # نستخدم asyncio لتشغيل هذه المهمة غير المتزامنة
        loop = asyncio.new_event_loop( )
        asyncio.set_event_loop(loop)
        try:
            # نقوم بتشغيل معالجات التطبيق في الخلفية
            application.initialize()
            # نضبط الـ Webhook
            loop.run_until_complete(application.bot.set_webhook(url=webhook_url, allowed_updates=telegram.Update.ALL_TYPES))
            print(f"✅ [Telegram] تم تعيين الـ Webhook بنجاح.", flush=True)
        except Exception as e:
            print(f"❌ فشل إعداد الـ Webhook: {e}", flush=True)
        # لا نغلق الحلقة هنا، ولكن هذا الإعداد يعمل مرة واحدة فقط
    else:
        print("⚠️ لم يتم العثور على RENDER_EXTERNAL_HOSTNAME. تخطي إعداد الـ Webhook (مناسب للاختبار المحلي).")

    # حذف الخيط الخاص بتليجرام
    # telegram_thread = threading.Thread(target=run_telegram_bot, name="TelegramBotThread")
    # telegram_thread.daemon = True
    # telegram_thread.start() # ==> تم الحذف

    scheduler = BackgroundScheduler()
    # scheduler.add_job(check_for_inactive_users, 'interval', minutes=5)
    scheduler.start()
    print("⏰ تم بدء الجدولة بنجاح.", flush=True)

    print("🚀 جاري بدء تشغيل سيرفر Flask (واتساب و تيليجرام)...", flush=True)
    port = int(os.environ.get("PORT", 5000))
    
    # لا نستخدم app.run() عند النشر على Render مع Gunicorn
    # هذا السطر للاختبار المحلي فقط إذا لم تستخدم Gunicorn
    # app.run(host="0.0.0.0", port=port, debug=False)
