# -*- coding: utf-8 -*-
import os
import time
import json
import requests
import threading
import asyncio
import logging
import base64
from flask import Flask, request, jsonify
from asgiref.wsgi import WsgiToAsgi
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# --- الإعدادات ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)
load_dotenv()
logger.info("▶️ [START] تم تحميل إعدادات البيئة.")

# --- مفاتيح API ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
MONGO_URI = os.getenv("MONGO_URI")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
logger.info("🔑 [CONFIG] تم تحميل مفاتيح API.")

# --- قاعدة البيانات ---
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ [DB] تم الاتصال بقاعدة البيانات بنجاح.")
except Exception as e:
    logger.critical(f"❌ [DB] فشل الاتصال بقاعدة البيانات: {e}", exc_info=True)
    exit()

# --- إعدادات التطبيق ---
flask_app = Flask(__name__)
app = WsgiToAsgi(flask_app)
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("🚀 [APP] تم إعداد تطبيق Flask و OpenAI Client.")

# --- متغيرات عالمية ---
pending_messages = {}
message_timers = {}
processing_locks = {}
BATCH_WAIT_TIME = 2.0

# --- دوال إدارة الجلسات ---
def get_or_create_session_from_contact(contact_data, platform):
    user_id = str(contact_data.get("id"))
    if not user_id:
        logger.error(f"❌ [SESSION] لم يتم العثور على user_id في البيانات: {contact_data}")
        return None
        
    session = sessions_collection.find_one({"_id": user_id})
    now_utc = datetime.now(timezone.utc)
    
    main_platform = "Unknown"
    if platform.startswith("ManyChat"):
        main_platform = "Instagram" if contact_data.get("ig_id") else "Facebook"
    elif platform == "Telegram":
        main_platform = "Telegram"

    if session:
        update_fields = {
            "last_contact_date": now_utc, "platform": main_platform,
            "profile.name": contact_data.get("name"), "profile.profile_pic": contact_data.get("profile_pic"),
            "status": "active"
        }
        sessions_collection.update_one({"_id": user_id}, {"$set": {k: v for k, v in update_fields.items() if v is not None}})
        return sessions_collection.find_one({"_id": user_id})
    else:
        logger.info(f"🆕 [SESSION] مستخدم جديد. جاري إنشاء جلسة شاملة له: {user_id}")
        new_session = {
            "_id": user_id, "platform": main_platform,
            "profile": {"name": contact_data.get("name"), "first_name": contact_data.get("first_name"), "last_name": contact_data.get("last_name"), "profile_pic": contact_data.get("profile_pic")},
            "openai_thread_id": None, "tags": [f"source:{main_platform.lower()}"],
            "custom_fields": contact_data.get("custom_fields", {}),
            "conversation_summary": "", "status": "active",
            "first_contact_date": now_utc, "last_contact_date": now_utc
        }
        sessions_collection.insert_one(new_session)
        return new_session

# --- دوال OpenAI (مع تعديل الـ Prompt) ---
async def get_image_description_for_assistant(base64_image):
    logger.info("🤖 [VISION-FOR-ASSISTANT] بدء استخلاص وصف تفصيلي من الصورة...")

    prompt_text = (
        "استخرج كل النصوص الموجودة في هذه الصورة بدقة شديدة وبشكل حرفي. "
        "اعرض التفاصيل بالكامل مثل المبالغ، أرقام الهواتف، التواريخ، وأي بيانات أخرى."
    )

    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4.1",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                    ],
                }
            ],
            max_tokens=500,
        )
        description = response.choices[0].message["content"][0]["text"]
        logger.info(f"✅ [VISION] النص المستخلص: {description}")
        return description
    except Exception as e:
        logger.error(f"❌ [VISION] فشل استخلاص النص من الصورة: {e}", exc_info=True)
        return None

async def get_assistant_reply(session, content):
    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")
    logger.info(f"🤖 [ASSISTANT] بدء عملية الحصول على رد للمستخدم {user_id}.")

    if not thread_id:
        logger.warning(f"🧵 [ASSISTANT] لا يوجد thread للمستخدم {user_id}. سيتم إنشاء واحد جديد.")
        try:
            thread = await asyncio.to_thread(client.beta.threads.create)
            thread_id = thread.id
            sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
            logger.info(f"✅ [ASSISTANT] تم إنشاء وتخزين thread جديد: {thread_id}")
        except Exception as e:
            logger.error(f"❌ [ASSISTANT] فشل في إنشاء thread جديد: {e}", exc_info=True)
            return "⚠️ عفوًا، حدث خطأ أثناء تهيئة المحادثة."
    
    try:
        await asyncio.to_thread(client.beta.threads.messages.create, thread_id=thread_id, role="user", content=content)
        logger.info(f"▶️ [ASSISTANT] بدء تشغيل المساعد (run) للـ thread: {thread_id}")
        run = await asyncio.to_thread(client.beta.threads.runs.create, thread_id=thread_id, assistant_id=ASSISTANT_ID_PREMIUM)
        
        start_time = time.time()
        while run.status in ["queued", "in_progress"]:
            if time.time() - start_time > 90:
                logger.error(f"⏰ [ASSISTANT] Timeout! استغرق الـ run {run.id} أكثر من 90 ثانية.")
                return "⚠️ حدث تأخير في الرد، يرجى المحاولة مرة أخرى."
            await asyncio.sleep(1)
            run = await asyncio.to_thread(client.beta.threads.runs.retrieve, thread_id=thread_id, run_id=run.id)

        if run.status == "completed":
            messages = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=1)
            reply = messages.data[0].content[0].text.value.strip()
            logger.info(f"🗣️ [ASSISTANT] الرد الذي تم الحصول عليه: \"{reply}\"")
            return reply
        else:
            logger.error(f"❌ [ASSISTANT] لم يكتمل الـ run. الحالة: {run.status}. الخطأ: {run.last_error}")
            return "⚠️ عفوًا، حدث خطأ فني. فريقنا يعمل على إصلاحه."
    except Exception as e:
        logger.error(f"❌ [ASSISTANT] حدث استثناء غير متوقع: {e}", exc_info=True)
        return "⚠️ عفوًا، حدث خطأ غير متوقع."

# --- دوال الإرسال والوسائط ---
def send_manychat_reply(subscriber_id, text_message):
    logger.info(f"📤 [MANYCHAT] بدء إرسال رد إلى {subscriber_id}...")
    if not MANYCHAT_API_KEY:
        logger.error("❌ [MANYCHAT] مفتاح MANYCHAT_API_KEY غير موجود!")
        return
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    payload = {"subscriber_id": str(subscriber_id ), "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message}]}}}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        logger.info(f"✅ [MANYCHAT] تم إرسال الرسالة بنجاح إلى {subscriber_id}.")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [MANYCHAT] فشل إرسال الرسالة: {e.response.text if e.response else e}", exc_info=True)

async def send_telegram_message(bot, chat_id, text, business_id=None):
    logger.info(f"📤 [TELEGRAM] بدء إرسال رسالة إلى {chat_id}...")
    try:
        if business_id:
            await bot.send_message(chat_id=chat_id, text=text, business_connection_id=business_id)
        else:
            await bot.send_message(chat_id=chat_id, text=text)
        logger.info(f"✅ [TELEGRAM] تم إرسال الرسالة بنجاح إلى {chat_id}.")
    except Exception as e:
        logger.error(f"❌ [TELEGRAM] فشل إرسال الرسالة إلى {chat_id}: {e}", exc_info=True)

def download_media_from_url(media_url):
    logger.info(f"⬇️ [MEDIA] محاولة تحميل وسائط من الرابط: {media_url}")
    try:
        media_response = requests.get(media_url, timeout=20)
        media_response.raise_for_status()
        logger.info(f"✅ [MEDIA] تم تحميل الوسائط بنجاح.")
        return media_response.content
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [MEDIA] فشل تحميل الوسائط من {media_url}: {e}", exc_info=True)
        return None

def transcribe_audio(audio_content, file_format="mp4"):
    logger.info(f"🎙️ [WHISPER] بدء تحويل مقطع صوتي (الصيغة: {file_format})...")
    try:
        temp_audio_file = f"temp_audio_{int(time.time())}.{file_format}"
        with open(temp_audio_file, "wb") as f: f.write(audio_content)
        with open(temp_audio_file, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        os.remove(temp_audio_file)
        logger.info(f"✅ [WHISPER] تم تحويل الصوت إلى نص بنجاح.")
        return transcription.text
    except Exception as e:
        logger.error(f"❌ [WHISPER] خطأ أثناء تحويل الصوت: {e}", exc_info=True)
        return None

# --- آلية المعالجة الموحدة ---
def schedule_assistant_response(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages or not pending_messages[user_id]: return
        
        user_data = pending_messages[user_id]
        session = user_data["session"]
        platform = session["platform"]
        combined_content = "\n".join(user_data["texts"])
        
        logger.info(f"⚙️ [BATCH] بدء معالجة المحتوى المجمع للمستخدم {user_id} على {platform}: '{combined_content}'")
        reply_text = asyncio.run(get_assistant_reply(session, combined_content))
        
        if reply_text:
            if platform in ["Instagram", "Facebook"]:
                send_manychat_reply(user_id, reply_text)
            elif platform == "Telegram":
                bot_instance = telegram_app.bot
                business_id = user_data.get("business_id")
                asyncio.run(send_telegram_message(bot_instance, user_id, reply_text, business_id))

        if user_id in pending_messages: del pending_messages[user_id]
        if user_id in message_timers: del message_timers[user_id]
        logger.info(f"🗑️ [BATCH] تم الانتهاء من المعالجة للمستخدم {user_id}.")

def add_to_processing_queue(session, text_content, **kwargs):
    user_id = session["_id"]
    if user_id in message_timers: message_timers[user_id].cancel()
    
    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session, **kwargs}
    
    pending_messages[user_id]["texts"].append(text_content)
    logger.info(f"➕ [QUEUE] تمت إضافة محتوى إلى قائمة الانتظار للمستخدم {user_id}. حجم القائمة الآن: {len(pending_messages[user_id]['texts'])}")
    
    timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

# --- ويب هوك ManyChat ---
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    try:
        data = request.json
        logger.info(f"📞 [WEBHOOK-MC] تم استلام طلب جديد: {json.dumps(data, ensure_ascii=False)}")

        # استخراج بيانات المستخدم والرسالة
        full_contact = data.get("contact", {})
        subscriber_id = full_contact.get("id")
        user_name = f"{full_contact.get('first_name', '')} {full_contact.get('last_name', '')}".strip()
        message_data = data.get("message", {})
        message_text = message_data.get("text", "")
        message_type = message_data.get("type", "text")

        # --- تحديد المنصة الفعلية (Facebook أو Instagram) ---
        platform_source = "Facebook"
        try:
            if "ig_id" in full_contact or "instagram" in json.dumps(full_contact).lower():
                platform_source = "Instagram"
        except Exception as e:
            logger.warning(f"⚠ [WEBHOOK-MC] فشل في تحديد المنصة تلقائيًا: {e}")

        logger.info(f"🌐 [WEBHOOK-MC] تم تحديد المنصة: {platform_source}")

        # إنشاء أو استرجاع الجلسة الخاصة بالمستخدم
        session = get_or_create_session_from_contact(full_contact, f"ManyChat-{platform_source}")

        # طباعة لتتبع نوع الرسالة
        logger.info(f"💬 [WEBHOOK-MC] نوع الرسالة: {message_type} - المحتوى: {message_text}")

        # التعامل مع أنواع الرسائل المختلفة
        if message_type == "image":
            image_url = message_data.get("image", {}).get("url")
            logger.info(f"🖼 [WEBHOOK-MC] تم استلام صورة من {user_name}: {image_url}")
            send_manychat_reply(subscriber_id, f"تم استلام الصورة ✅", platform=platform_source)

        elif message_type == "audio":
            audio_url = message_data.get("audio", {}).get("url")
            logger.info(f"🎧 [WEBHOOK-MC] تم استلام مقطع صوتي من {user_name}: {audio_url}")
            send_manychat_reply(subscriber_id, f"تم استلام المقطع الصوتي 🎵", platform=platform_source)

        elif message_text:
            logger.info(f"🗨 [WEBHOOK-MC] تم استلام رسالة نصية من {user_name}: {message_text}")
            process_user_message(subscriber_id, message_text, platform_source)

        else:
            logger.warning(f"⚠ [WEBHOOK-MC] لم يتم التعرف على نوع الرسالة من {user_name}")
            send_manychat_reply(subscriber_id, "لم أفهم الرسالة دي، ممكن توضحلي أكتر؟ 🤔", platform=platform_source)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.error(f"❌ [WEBHOOK-MC] خطأ أثناء معالجة الطلب: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# --- منطق تيليجرام ---
if TELEGRAM_BOT_TOKEN:
    logger.info("🔌 [TELEGRAM] تم العثور على توكن تليجرام. جاري إعداد البوت...")
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    async def start_command(update, context):
        await update.message.reply_text(f"أهلاً {update.effective_user.first_name}!")

    async def handle_telegram_message(update, context):
        message = update.message or update.business_message
        if not message: return
        
        user_contact_data = {"id": message.from_user.id, "name": message.from_user.full_name, "first_name": message.from_user.first_name, "last_name": message.from_user.last_name}
        session = get_or_create_session_from_contact(user_contact_data, "Telegram")
        if not session: return

        business_id = getattr(update.business_message, "business_connection_id", None) if hasattr(update, "business_message") and update.business_message else None
        
        def background_task():
            content_for_assistant = None
            if message.text:
                content_for_assistant = message.text
            elif message.voice:
                voice_file = asyncio.run(message.voice.get_file())
                voice_content = asyncio.run(voice_file.download_as_bytearray())
                transcribed_text = transcribe_audio(bytes(voice_content), file_format="ogg")
                if transcribed_text: content_for_assistant = f"[رسالة صوتية من العميل]: {transcribed_text}"
            elif message.photo:
                photo_file = asyncio.run(message.photo[-1].get_file())
                photo_content = asyncio.run(photo_file.download_as_bytearray())
                base64_image = base64.b64encode(bytes(photo_content)).decode('utf-8')
                description = asyncio.run(get_image_description_for_assistant(base64_image))
                if description:
                    caption = message.caption or ""
                    content_for_assistant = f"[وصف صورة أرسلها العميل]: {description}\n[تعليق العميل على الصورة]: {caption}"
            
            if content_for_assistant:
                add_to_processing_queue(session, content_for_assistant, business_id=business_id)

        threading.Thread(target=background_task).start()

    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_telegram_message))

    @flask_app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
    async def telegram_webhook_handler():
        data = request.get_json()
        update = telegram.Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return jsonify({"status": "ok"})
    logger.info("✅ [TELEGRAM] تم إعداد معالجات تليجرام والويب هوك بنجاح.")

# --- نقطة الدخول الرئيسية ---
@flask_app.route("/")
def home():
    return "✅ Bot is running with Detailed Vision Logic (v10 - Full Integration)."

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل. يرجى استخدام خادم WSGI (مثل Gunicorn) لتشغيله في بيئة الإنتاج.")
