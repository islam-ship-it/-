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
# تحميل متغيرات البيئة من ملف .env
# ==============================================================================
load_dotenv()

# ==============================================================================
# إعدادات البيئة
# ==============================================================================
# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")

# WhatsApp (ZAPI)
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# MongoDB
MONGO_URI = os.getenv("MONGO_URI")

# Follow-up
FOLLOW_UP_INTERVAL_MINUTES = int(os.getenv("FOLLOW_UP_INTERVAL_MINUTES", 1440))
MAX_FOLLOW_UPS = int(os.getenv("MAX_FOLLOW_UPS", 3))

# ==============================================================================
# إعدادات قاعدة البيانات (MongoDB)
# ==============================================================================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"] # اسم قاعدة بيانات جديد ليعكس تعدد المنصات
    sessions_collection = db["sessions"]
    print("✅ تم الاتصال بقاعدة البيانات بنجاح.", flush=True)
except Exception as e:
    print(f"❌ فشل الاتصال بقاعدة البيانات: {e}", flush=True)
    exit() # إيقاف التطبيق إذا فشل الاتصال بقاعدة البيانات

# ==============================================================================
# إعداد تطبيق Flask وعميل OpenAI
# ==============================================================================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

# ==============================================================================
# متغيرات عالمية لإدارة الرسائل المعلقة والمؤقتات والـ Locks
# ==============================================================================
pending_messages = {}
timers = {}
thread_locks = {}
client_processing_locks = {}

# ==============================================================================
# دوال إدارة الجلسات (مشتركة بين المنصات)
# ==============================================================================
def get_session(user_id):
    """
    يسترجع بيانات جلسة المستخدم من قاعدة البيانات أو ينشئ جلسة جديدة.
    """
    session = sessions_collection.find_one({"_id": user_id})
    if not session:
        session = {
            "_id": user_id, "history": [], "thread_id": None, "message_count": 0,
            "name": "", "last_message_time": datetime.utcnow().isoformat(),
            "follow_up_sent": 0, "follow_up_status": "none", "last_follow_up_time": None,
            "payment_status": "pending"
        }
    # التأكد من وجود المفاتيح الافتراضية في الجلسات القديمة
    session.setdefault("last_message_time", datetime.utcnow().isoformat())
    session.setdefault("follow_up_sent", 0)
    session.setdefault("follow_up_status", "none")
    return session

def save_session(user_id, session_data):
    """
    يحفظ أو يحدث بيانات جلسة المستخدم في قاعدة البيانات.
    """
    session_data["_id"] = user_id
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)
    print(f"💾 تم حفظ بيانات الجلسة للمستخدم {user_id}.", flush=True)

# ==============================================================================
# دوال إرسال الرسائل (خاصة بكل منصة)
# ==============================================================================
def send_whatsapp_message(phone, message):
    """
    يرسل رسالة نصية إلى رقم هاتف محدد باستخدام ZAPI.
    """
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
    """
    يرسل رسالة نصية إلى محادثة محددة في تيليجرام.
    """
    try:
        await context.bot.send_message(chat_id=chat_id, text=message)
        print(f"📤 [Telegram] تم إرسال رسالة للعميل {chat_id}.", flush=True)
    except Exception as e:
        print(f"❌ [Telegram] خطأ أثناء إرسال الرسالة: {e}", flush=True)

# ==============================================================================
# دوال مشتركة (تحويل الصوت، التفاعل مع المساعد)
# ==============================================================================
def transcribe_audio(audio_url, file_format="ogg"):
    """
    يحمل ملف صوتي من URL ويحوله إلى نص باستخدام OpenAI Whisper API.
    """
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
        traceback.print_exc()
        return None

def ask_assistant(content, sender_id, name=""):
    """
    يرسل المحتوى إلى مساعد OpenAI ويسترجع الرد. (دالة مشتركة)
    """
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

    if session["thread_id"] not in thread_locks:
        thread_locks[session["thread_id"]] = threading.Lock()

    with thread_locks[session["thread_id"]]:
        try:
            client.beta.threads.messages.create(thread_id=session["thread_id"], role="user", content=content)
            run = client.beta.threads.runs.create(thread_id=session["thread_id"], assistant_id=ASSISTANT_ID_PREMIUM)
            
            while True:
                run_status = client.beta.threads.runs.retrieve(thread_id=session["thread_id"], run_id=run.id)
                if run_status.status == "completed":
                    break
                elif run_status.status in ["failed", "cancelled", "expired"]:
                    print(f"❌ الـ Run فشل أو تم إلغاؤه/انتهت صلاحيته: {run_status.status}", flush=True)
                    return "⚠ حدث خطأ أثناء معالجة طلبك، حاول مرة أخرى."
                time.sleep(1)
            
            messages = client.beta.threads.messages.list(thread_id=session["thread_id"])
            reply = messages.data[0].content[0].text.value.strip()
            
            session["history"].append({"role": "user", "content": content})
            session["history"].append({"role": "assistant", "content": reply})
            session["history"] = session["history"][-10:]
            save_session(sender_id, session)
            
            return reply
        except Exception as e:
            print(f"❌ حصل استثناء أثناء الإرسال للمساعد أو استلام الرد: {e}", flush=True)
            traceback.print_exc()
            return "⚠ مشكلة مؤقتة، حاول مرة أخرى."

# ==============================================================================
# منطق WhatsApp (Flask Webhook)
# ==============================================================================
def process_whatsapp_messages(sender, name):
    """
    تجمع رسائل واتساب النصية وترسلها للمساعد.
    """
    with client_processing_locks.setdefault(sender, threading.Lock()):
        time.sleep(8) # الانتظار لتجميع الرسائل
        if not pending_messages.get(sender):
            timers.pop(sender, None)
            return

        combined_text = "\n".join(pending_messages[sender])
        content = [{"type": "text", "text": combined_text}]
        
        reply = ask_assistant(content, sender, name)

        # محاكاة تأخير الكتابة البشري
        typing_delay = max(1, min(len(reply) / 5.0, 8))
        print(f"⏳ [WhatsApp] محاكاة تأخير الكتابة لمدة {typing_delay:.2f} ثانية للعميل {sender}", flush=True)
        time.sleep(typing_delay)

        send_whatsapp_message(sender, reply)
        
        pending_messages[sender] = []
        timers.pop(sender, None)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    sender = data.get("phone")
    if not sender: return jsonify({"status": "no sender"}), 400

    session = get_session(sender)
    session["last_message_time"] = datetime.utcnow().isoformat()
    session["follow_up_sent"] = 0 # إعادة تصفير المتابعة عند رد العميل
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
        if sender not in pending_messages: pending_messages[sender] = []
        pending_messages[sender].append(msg)
        if sender not in timers:
            timers[sender] = threading.Thread(target=process_whatsapp_messages, args=(sender, name))
            timers[sender].start()
            
    return jsonify({"status": "received"}), 200

# ==============================================================================
# منطق Telegram (python-telegram-bot)
# ==============================================================================
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
    if update.message.text:
        reply = ask_assistant(update.message.text, chat_id, user_name)
    elif update.message.voice:
        voice_file = await update.message.voice.get_file()
        transcribed_text = transcribe_audio(voice_file.file_path)
        if transcribed_text:
            reply = ask_assistant(f"رسالة صوتية من العميل: {transcribed_text}", chat_id, user_name)
        else:
            reply = "عذراً، لم أتمكن من فهم رسالتك الصوتية."
    elif update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        caption = update.message.caption or ""
        content = [{"type": "image_url", "image_url": {"url": photo_file.file_path}}]
        if caption: content.append({"type": "text", "text": f"تعليق على الصورة: {caption}"})
        reply = ask_assistant(content, chat_id, user_name)

    if reply:
        await send_telegram_message(context, chat_id, reply)

def run_telegram_bot():
    """إعداد وتشغيل بوت تيليجرام."""
    print("🚀 جاري بدء تشغيل بوت تيليجرام...", flush=True)
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.PHOTO, handle_telegram_message))
    application.run_polling()

# ==============================================================================
# نظام المتابعة التلقائية (Scheduler)
# ==============================================================================
def check_for_inactive_users():
    # هذا الجزء لم يتم تعديله، يمكنك تفعيله إذا أردت
    pass 

# ==============================================================================
# تشغيل التطبيق
# ==============================================================================
if __name__ == "__main__":
    # إعداد الجدولة
    scheduler = BackgroundScheduler()
    # scheduler.add_job(check_for_inactive_users, 'interval', minutes=5) # قم بإلغاء التعليق لتفعيله
    scheduler.start()
    print("⏰ تم بدء الجدولة بنجاح.", flush=True)

    # تشغيل بوت تيليجرام في خيط منفصل
    telegram_thread = threading.Thread(target=run_telegram_bot)
    telegram_thread.daemon = True
    telegram_thread.start()

    # تشغيل تطبيق Flask (واتساب)
    print("🚀 جاري بدء تشغيل سيرفر واتساب (Flask)...", flush=True)
    app.run(host="0.0.0.0", port=5000, debug=False) # يفضل استخدام debug=False في الإنتاج
