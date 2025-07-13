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

# --- (كل إعدادات البيئة وقاعدة البيانات تبقى كما هي) ---
# ... (سأختصرها هنا ولكنها موجودة في الكود الكامل أدناه) ...

# الكود الكامل يبدأ هنا
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
    print("❌ خطأ فادح: واحد أو أكثر من متغيرات البيئة الأساسية غير موجود.")

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
    print(f"💾 تم حفظ بيانات الجلسة للمستخدم {user_id_str}.", flush=True)

# ==============================================================================
# دوال إرسال الرسائل
# ==============================================================================
def send_whatsapp_message(phone, message):
    # ... (الكود يبقى كما هو)
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        print(f"📤 [WhatsApp] تم إرسال رسالة للعميل {phone}، الحالة: {response.status_code}", flush=True)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"❌ [WhatsApp] خطأ أثناء إرسال الرسالة عبر ZAPI: {e}", flush=True)

# دالة إرسال تيليجرام أصبحت أبسط
async def send_telegram_message(bot, chat_id, message):
    try:
        await bot.send_message(chat_id=chat_id, text=message)
        print(f"📤 [Telegram] تم إرسال رسالة للعميل {chat_id}.", flush=True)
    except Exception as e:
        print(f"❌ [Telegram] خطأ أثناء إرسال الرسالة: {e}", flush=True)

# ==============================================================================
# دوال مشتركة (تحويل الصوت، التفاعل مع المساعد)
# ==============================================================================
def transcribe_audio(audio_url, file_format="ogg"):
    # ... (الكود يبقى كما هو)
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
    # ... (الكود يبقى كما هو)
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
# منطق WhatsApp (Flask Webhook)
# ==============================================================================
def process_whatsapp_messages(sender, name):
    # ... (الكود يبقى كما هو)
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
    # ... (الكود يبقى كما هو)
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
# منطق Telegram (Webhook) - الإصدار الجديد والمستقر
# ==============================================================================

# دالة المعالجة الرئيسية لتليجرام
async def handle_telegram_update(update_data):
    """
    تعالج تحديث تيليجرام واحداً في كل مرة، مع إدارة حلقة الأحداث الخاصة بها.
    """
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    update = telegram.Update.de_json(update_data, bot)

    # *** إصلاح خطأ AttributeError ***
    # التحقق من أن التحديث هو رسالة جديدة قبل المتابعة
    if not update.message:
        print("ℹ️ [Telegram] تم استلام تحديث ليس رسالة (مثل تعديل رسالة)، سيتم تجاهله.")
        return

    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name
    
    print(f"🕵️‍♂️ [Telegram] بدأت معالجة رسالة من {user_name} ({chat_id}).")
    
    session = get_session(chat_id)
    session["last_message_time"] = datetime.utcnow().isoformat()
    save_session(chat_id, session)

    await bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)
    
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
        print("🧠 [Telegram] جاري طلب رد من المساعد...")
        reply = ask_assistant(content_for_assistant, chat_id, user_name)
        print(f"💬 [Telegram] الرد المستلم من المساعد: '{reply}'")

    if reply:
        await send_telegram_message(bot, chat_id, reply)
    else:
        print("⚠️ [Telegram] لا يوجد رد لإرساله.")

# مسار الـ Webhook الجديد
@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def telegram_webhook_handler():
    """
    يستقبل الطلب من تيليجرام ويقوم بتشغيل المعالج في حلقة أحداث جديدة.
    """
    update_data = request.get_json()
    print(f"📥 [Telegram Webhook] بيانات مستلمة.", flush=True)
    
    # *** إصلاح خطأ Event loop is closed ***
    # نقوم بتشغيل دالة المعالجة غير المتزامنة في حلقة أحداث جديدة لكل طلب
    try:
        asyncio.run(handle_telegram_update(update_data))
    except Exception as e:
        print(f"❌ خطأ فادح في معالج تيليجرام: {e}", flush=True)
        traceback.print_exc()

    return jsonify({"status": "ok"})

# دالة إعداد الـ Webhook (تعمل مرة واحدة فقط)
async def setup_telegram_webhook():
    render_hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME')
    if render_hostname:
        print("🔧 جاري إعداد Webhook تيليجرام...", flush=True)
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        webhook_url = f"https://{render_hostname}/{TELEGRAM_BOT_TOKEN}"
        await bot.set_webhook(url=webhook_url, allowed_updates=["message"] )
        print(f"✅ [Telegram] تم إعداد الـ Webhook بنجاح على: {webhook_url}", flush=True)
    else:
        print("⚠️ لم يتم العثور على RENDER_EXTERNAL_HOSTNAME. تخطي إعداد الـ Webhook.", flush=True)

# نقوم بتشغيل دالة الإعداد عند بدء تشغيل السيرفر
try:
    print("⏳ محاولة إعداد Webhook تيليجرام...", flush=True)
    asyncio.run(setup_telegram_webhook())
except Exception as e:
    print(f"❌ فشل إعداد تيليجرام أثناء بدء التشغيل: {e}", flush=True)

# ==============================================================================
# المسار الرئيسي ونظام الجدولة
# ==============================================================================
@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر يعمل (واتساب و تيليجرام)."

def check_for_inactive_users():
    pass 

scheduler = BackgroundScheduler()
# scheduler.add_job(check_for_inactive_users, 'interval', minutes=5)
scheduler.start()
print("⏰ تم بدء الجدولة بنجاح.", flush=True)

# ==============================================================================
# تشغيل التطبيق
# ==============================================================================
if __name__ == "__main__":
    # هذا الجزء يستخدم فقط للاختبار المحلي المباشر بدون Gunicorn
    print("🚀 جاري بدء تشغيل السيرفر للاختبار المحلي (لا تستخدم هذا في الإنتاج)...")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
