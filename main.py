import os
import threading
import time
import asyncio
import random
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# MongoDB setup
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
client = MongoClient(MONGO_URI)
db = client.whatsapp_bot
sessions_collection = db.sessions

# OpenAI setup (assuming you have your OpenAI API key set as an environment variable)
# from openai import OpenAI
# client_openai = OpenAI()

# ZAPI setup
ZAPI_API_URL = os.getenv("ZAPI_API_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_API_TOKEN = os.getenv("ZAPI_API_TOKEN")

# Telegram setup
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Flask app setup
app = Flask(__name__)

# Threading locks for session management
session_locks = {}

# --- Helper Functions ---

def get_session_lock(sender_id):
    if sender_id not in session_locks:
        session_locks[sender_id] = threading.Lock()
    return session_locks[sender_id]

def get_session(sender_id):
    session = sessions_collection.find_one({"sender_id": str(sender_id)})
    if not session:
        session = {
            "sender_id": str(sender_id),
            "thread_id": None,  # OpenAI Assistant Thread ID
            "last_message_time": datetime.utcnow().isoformat(),
            "follow_up_sent": 0,
            "follow_up_status": "active", # active, responded, paid, closed
            "messages": []
        }
        sessions_collection.insert_one(session)
    return session

def save_session(sender_id, session_data):
    sessions_collection.update_one(
        {"sender_id": str(sender_id)},
        {"$set": session_data},
        upsert=True
    )

def transcribe_audio(audio_file_path, file_format="ogg"):
    # This function would typically use an external API like OpenAI\"s Whisper
    # For demonstration, we\"ll just return a placeholder
    print(f"🎙️ Transcribing audio file: {audio_file_path} ({file_format})", flush=True)
    return "هذا نص تجريبي من رسالة صوتية."

# --- ZAPI (WhatsApp) Functions ---

def send_message(to, message):
    url = f"{ZAPI_API_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_API_TOKEN}/send-message"
    headers = {"Content-Type": "application/json"}
    payload = {
        "to": to,
        "body": message
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        print(f"📤 تم إرسال رسالة إلى {to}: {message}", flush=True)
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ خطأ في إرسال رسالة عبر ZAPI: {e}", flush=True)
        return None

# Message buffering for WhatsApp
pending_messages = {}
pending_message_timers = {}

def process_pending_messages(sender, name):
    with get_session_lock(sender):
        if sender in pending_messages and pending_messages[sender]:
            print(f"⏳ معالجة الرسائل المعلقة للعميل {sender}", flush=True)
            combined_text = "\n".join(pending_messages[sender])
            content = combined_text

            # Simulate typing delay for WhatsApp
            reply = ask_assistant(content, sender, name)
            typing_delay = len(reply) / 5.0  # Assume 5 chars per second typing speed
            final_delay = max(1, min(typing_delay, 8)) # Delay between 1 and 8 seconds
            print(f"⏳ محاكاة تأخير الكتابة لمدة {final_delay:.2f} ثانية للعميل {sender}", flush=True)
            time.sleep(final_delay)

            send_message(sender, reply)
            pending_messages[sender] = []
            if sender in pending_message_timers:
                pending_message_timers[sender].cancel()
                del pending_message_timers[sender]

# --- OpenAI Assistant Interaction ---

def ask_assistant(message_content, sender_id, user_name):
    # This is a placeholder for actual OpenAI Assistant interaction
    # In a real scenario, you would use client_openai.beta.threads.messages.create
    # and client_openai.beta.threads.runs.create

    session = get_session(sender_id)
    # if not session[\"thread_id\"]:
    #     thread = client_openai.beta.threads.create()
    #     session[\"thread_id\"] = thread.id
    #     save_session(sender_id, session)

    print(f"🧠 جاري طلب رد من المساعد للعميل {sender_id}...", flush=True)

    # Simulate assistant response
    if isinstance(message_content, list):
        text_parts = [item["text"] for item in message_content if item["type"] == "text"]
        image_parts = [item["image_url"]["url"] for item in message_content if item["type"] == "image_url"]
        combined_input = " ".join(text_parts) + (" (مع صور)" if image_parts else "")
    else:
        combined_input = message_content

    # Simple rule-based response for demonstration
    if "مرحباً" in combined_input or "أهلاً" in combined_input:
        reply = f"أهلاً بك يا {user_name}! كيف يمكنني مساعدتك اليوم؟"
    elif "شكراً" in combined_input:
        reply = "على الرحب والسعة!"
    elif "صورة" in combined_input:
        reply = "لقد استلمت الصورة. كيف يمكنني مساعدتك بخصوصها؟"
    elif "صوت" in combined_input or "ريكورد" in combined_input:
        reply = "لقد استلمت رسالتك الصوتية. كيف يمكنني مساعدتك بخصوصها؟"
    else:
        reply = "أهلًا بحضرتك مجددًا 😊\nهل في حاجة معينة تحب تفسر عنها أو تريد تكمّل الطلب؟ \n" اقرأ أساعد حضرتك في أي وقت."

    print(f"💬 الرد المستلم من المساعد: \'{reply}\'", flush=True)
    return reply

# --- Flask Webhooks ---

@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    print("📥 [WhatsApp Webhook] بيانات مستلمة.", flush=True)
    data = request.json
    # print(f"بيانات واتساب: {data}", flush=True)

    if data and "messages" in data:
        for message in data["messages"]:
            sender = message["from"]
            message_type = message["type"]
            name = message.get("senderName", "عميل")

            with get_session_lock(sender):
                session = get_session(sender)
                session["last_message_time"] = datetime.utcnow().isoformat()
                session["follow_up_sent"] = 0 # Reset follow-up counter on any user message
                session["follow_up_status"] = "responded" # Change status to responded
                save_session(sender, session)

                print(f"🕵️‍♂️ [واتساب] بدأت رسالة من {name} ({sender}).", flush=True)

                if message_type == "text":
                    text_content = message["body"]
                    print(f"💬 رسالة نصية: {text_content}", flush=True)
                    if sender not in pending_messages:
                        pending_messages[sender] = []
                    pending_messages[sender].append(text_content)

                    if sender in pending_message_timers:
                        pending_message_timers[sender].cancel()
                    pending_message_timers[sender] = threading.Timer(
                        8.0, process_pending_messages, args=[sender, name]
                    )
                    pending_message_timers[sender].start()

                elif message_type == "image":
                    image_url = message["body"]
                    caption = message.get("caption", "")
                    print(f"🖼️ صورة: {image_url} (تعليق: {caption})", flush=True)
                    content = [
                        {"type": "text", "text": f"صورة من العميل {name} ({sender})."},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                    if caption:
                        content.append({"type": "text", "text": f"تعليق على الصورة:\n{caption}"})

                    reply = ask_assistant(content, sender, name)
                    send_message(sender, reply)

                elif message_type == "audio":
                    audio_url = message["body"]
                    print(f"🎙️ رسالة صوتية: {audio_url}", flush=True)
                    # In a real scenario, you\"d download the audio and transcribe it
                    transcribed_text = transcribe_audio(audio_url, file_format="ogg") # Assuming ZAPI provides ogg
                    if transcribed_text:
                        content = f"رسالة صوتية من العميل {name} ({sender}):\n{transcribed_text}"
                        reply = ask_assistant(content, sender, name)
                        send_message(sender, reply)
                    else:
                        send_message(sender, "عذراً، لم أتمكن من فهم رسالتك الصوتية. هل يمكنك كتابتها من فضلك؟")

                else:
                    print(f"⚠️ نوع رسالة غير مدعوم: {message_type}", flush=True)
                    send_message(sender, "عذراً، لا أستطيع معالجة هذا النوع من الرسائل حالياً.")

    return jsonify({"status": "received"}), 200

# --- Telegram Functions ---

async def send_telegram_message(chat_id, message):
    """
    يرسل رسالة نصية إلى محادثة محددة في تيليجرام.
    """
    try:
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=chat_id, text=message)
        print(f"📤 [تيليجرام] تم إرسال رسالة إلى الرقم {chat_id}.", flush=True)
    except Exception as e:
        print(f"❌ خطأ أثناء إرسال الرسالة عبر تيليجرام: {e}", flush=True)

async def start(update, context):
    """Handler لـ أمر /start."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id, text=f"مرحباً {user.first_name}! أنا مساعدك الآلي. كيف يمكنني مساعدتك اليوم؟")

async def handle_text_message(update, context):
    """Handler للرسائل النصية."""
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name
    message_text = update.message.text

    print(f"💬 استقبال رسالة نصية من {user_name} ({chat_id}) على تيليجرام: {message_text}", flush=True)

    # Simulate typing and delay
    await context.bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)
    delay_duration = random.uniform(1, 4)
    await asyncio.sleep(delay_duration)

    session = get_session(chat_id)
    session["last_message_time"] = datetime.utcnow().isoformat()
    save_session(chat_id, session)

    reply = ask_assistant(message_text, chat_id, user_name)
    await send_telegram_message(chat_id, reply)

async def handle_voice_message(update, context):
    """Handler للرسائل الصوتية (الريكوردات)."""
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name
    voice = update.message.voice

    print(f"🎙️ ريكورد صوتي مستلم من {user_name} ({chat_id}) على تيليجرام.", flush=True)

    try:
        voice_file = await voice.get_file()
        # Telegram handles files as ogg by default
        transcribed_text = transcribe_audio(voice_file.file_path, file_format="ogg")

        if transcribed_text:
            content = f"رسالة صوتية من العميل {user_name} ({chat_id}):\n{transcribed_text}"
            reply = ask_assistant(content, chat_id, user_name)
            await send_telegram_message(chat_id, reply)
        else:
            await send_telegram_message(chat_id, "عذراً، لم أتمكن من فهم رسالتك الصوتية. هل يمكنك كتابتها من فضلك؟")

    except Exception as e:
        print(f"❌ خطأ في معالجة رسالة تيليجرام الصوتية: {e}", flush=True)
        await send_telegram_message(chat_id, "حدث خطأ أثناء معالجة رسالتك الصوتية.")

async def handle_photo_message(update, context):
    """Handler للصور."""
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name
    caption = update.message.caption or ""

    print(f"🖼️ صورة مستلمة من {user_name} ({chat_id}) على تيليجرام.", flush=True)

    try:
        photo_file = await update.message.photo[-1].get_file()
        image_url = photo_file.file_path

        message_content = [
            {"type": "text", "text": f"صورة من العميل {user_name} ({chat_id})."},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]
        if caption:
            message_content.append({"type": "text", "text": f"تعليق على الصورة:\n{caption}"})

        reply = ask_assistant(message_content, chat_id, user_name)
        await send_telegram_message(chat_id, reply)

    except Exception as e:
        print(f"❌ خطأ في معالجة صورة تيليجرام: {e}", flush=True)
        await send_telegram_message(chat_id, "حدث خطأ أثناء معالجة الصورة.")

# New webhook handler for Telegram Business messages
@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def telegram_webhook_handler():
    print("📥 [Telegram Webhook] البيانات مستلمة.", flush=True)
    # Ensure the bot object is available globally or passed correctly
    global bot 
    if not hasattr(telegram_webhook_handler, 'bot_instance'):
        telegram_webhook_handler.bot_instance = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    bot = telegram_webhook_handler.bot_instance

    update = telegram.Update.de_json(request.json, bot)
    
    # Check if it\"s a business message or edited business message
    if update.business_message:
        print(f"🕵️‍♂️ [تلغرام] بدأت رسالة عمل من {update.business_message.chat.id}.", flush=True)
        # Process business message
        # For now, we\"ll treat it as a regular text message
        message_text = update.business_message.text
        chat_id = update.business_message.chat.id
        user_name = update.business_message.chat.first_name or "عميل عمل"
        
        # Simulate typing and delay
        await bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)
        delay_duration = random.uniform(1, 4)
        await asyncio.sleep(delay_duration)

        session = get_session(chat_id)
        session["last_message_time"] = datetime.utcnow().isoformat()
        save_session(chat_id, session)

        reply = ask_assistant(message_text, chat_id, user_name)
        await send_telegram_message(chat_id, reply)

    elif update.edited_business_message:
        print(f"🕵️‍♂️ [تلغرام] بدأت رسالة عمل معدلة من {update.edited_business_message.chat.id}.", flush=True)
        # Process edited business message
        # For now, we\"ll treat it as a regular text message
        message_text = update.edited_business_message.text
        chat_id = update.edited_business_message.chat.id
        user_name = update.edited_business_message.chat.first_name or "عميل عمل"
        
        # Simulate typing and delay
        await bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)
        delay_duration = random.uniform(1, 4)
        await asyncio.sleep(delay_duration)

        session = get_session(chat_id)
        session["last_message_time"] = datetime.utcnow().isoformat()
        save_session(chat_id, session)

        reply = ask_assistant(message_text, chat_id, user_name)
        await send_telegram_message(chat_id, reply)

    elif update.message:
        # Handle regular messages (text, voice, photo)
        if update.message.text:
            await handle_text_message(update, None) # Pass None for context as it\"s not needed here
        elif update.message.voice:
            await handle_voice_message(update, None)
        elif update.message.photo:
            await handle_photo_message(update, None)

    return jsonify({"status": "ok"}), 200

# --- Scheduler (Placeholder) ---

from apscheduler.schedulers.background import BackgroundScheduler

def check_for_inactive_users():
    print("⏰ جاري التحقق من المستخدمين غير النشطين...", flush=True)
    # Implement your logic here to find inactive users and send follow-up messages
    # Example: Find sessions where last_message_time is older than 24 hours
    # and follow_up_sent < 3
    pass

# --- Main Execution ---

def run_telegram_bot():
    """
    تقوم بإعداد وتشغيل بوت تيليجرام.
    """
    global bot # Make bot accessible globally
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add handlers for direct messages to the bot
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))

    # Set webhook for Telegram Business messages
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL") # Ensure this env var is set on Render
    if webhook_url:
        print(f"Setting Telegram webhook to: {webhook_url}/{TELEGRAM_BOT_TOKEN}", flush=True)
        # Corrected allowed_updates
        loop = asyncio.get_event_loop()
        loop.run_until_complete(bot.set_webhook(url=f"{webhook_url}/{TELEGRAM_BOT_TOKEN}", allowed_updates=["message", "business_message", "edited_business_message"]))
        print("✅ تم تعيين الـ Webhook بنجاح.", flush=True)
    else:
        print("⚠️ TELEGRAM_WEBHOOK_URL غير محدد. لن يتم تعيين الـ Webhook.", flush=True)

    # Start polling for direct messages (if webhook is not set or for local testing)
    # application.run_polling() # This should not be run if webhook is used
    print("✅ بوت تيليجرام جاهز لاستقبال الرسائل.", flush=True)

if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    # scheduler.add_job(check_for_inactive_users, \"interval\", minutes=5)
    scheduler.start()
    print("⏰ تم بدء الجدولة بنجاح.", flush=True)

    # Run Telegram bot setup in a separate thread
    telegram_thread = threading.Thread(target=run_telegram_bot)
    telegram_thread.daemon = True
    telegram_thread.start()

    # Run Flask app
    app.run(host="0.0.0.0", port=5000, debug=True)
