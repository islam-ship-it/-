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
app = Flask(__name__)
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
        contact_source = contact_data.get("source", "").lower()
        if "instagram" in contact_source:
            main_platform = "Instagram"
        elif "facebook" in contact_source:
            main_platform = "Facebook"
        else:
            main_platform = "Instagram" if "ig_id" in contact_data and contact_data.get("ig_id") else "Facebook"
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
        logger.info(f"🆕 [SESSION] مستخدم جديد. جاري إنشاء جلسة شاملة له: {user_id} على منصة {main_platform}")
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

# --- دوال OpenAI ---
async def get_image_description_for_assistant(base64_image):
    logger.info("🤖 [VISION-FOR-ASSISTANT] بدء استخلاص وصف تفصيلي من الصورة...")
    prompt_text = "استخرج كل النصوص الموجودة في هذه الصورة بدقة شديدة وبشكل حرفي. اعرض التفاصيل بالكامل مثل المبالغ، أرقام الهواتف، التواريخ، وأي بيانات أخرى."
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4.1",
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt_text}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
            max_tokens=500,
        )
        description = response.choices[0].message.content
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
def send_manychat_reply(subscriber_id, text_message, platform, retry=False):
    logger.info(f"📤 [MANYCHAT] بدء إرسال رد إلى {subscriber_id} على منصة {platform}...")
    if not MANYCHAT_API_KEY:
        logger.error("❌ [MANYCHAT] مفتاح MANYCHAT_API_KEY غير موجود!")
        return

    if platform not in ["Instagram", "Facebook"]:
        logger.error(f"❌ [MANYCHAT] منصة غير مدعومة أو غير محددة: '{platform}'. لا يمكن إرسال الرد.")
        return

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform == "Instagram" else "facebook"

    # تقسيم الرسالة لفقرات
    paragraphs = [p.strip( ) for p in text_message.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [p.strip() for p in text_message.split("\n") if p.strip()]
    messages_to_send = [{"type": "text", "text": p} for p in paragraphs] if paragraphs else []

    if not messages_to_send:
        logger.warning(f"⚠️ [MANYCHAT] لا يوجد محتوى لإرساله إلى {subscriber_id} بعد معالجة النص.")
        return

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"messages": messages_to_send}},
        "channel": channel,
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=20)
        response.raise_for_status()
        logger.info(f"✅ [MANYCHAT] تم إرسال الرسالة بنجاح إلى {subscriber_id} عبر {channel}.")

    except requests.exceptions.HTTPError as e:
        error_text = e.response.text if e.response is not None else str(e)
        # التحقق من كود الخطأ 3011
        if "3011" in error_text:
            if not retry:
                logger.warning(f"⚠️ [MANYCHAT] المستخدم {subscriber_id} خارج نافذة 24 ساعة أو لم يتم تحديث النشاط بعد. سيتم إعادة المحاولة بعد ثانيتين...")
                time.sleep(2)
                send_manychat_reply(subscriber_id, text_message, platform, retry=True)
            else:
                logger.error(f"❌ [MANYCHAT] فشل الإرسال للمستخدم {subscriber_id} حتى بعد إعادة المحاولة. تفاصيل الخطأ: {error_text}")
            return
        # أي خطأ آخر
        try:
            error_details = e.response.json()
        except Exception:
            error_details = error_text
        logger.error(f"❌ [MANYCHAT] فشل إرسال الرسالة: {e}. تفاصيل الخطأ: {error_details}", exc_info=True)

    except Exception as e:
        logger.error(f"❌ [MANYCHAT] خطأ غير متوقع أثناء الإرسال: {e}", exc_info=True)


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
                send_manychat_reply(user_id, reply_text, platform=platform)
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
    logger.info("📞 [WEBHOOK-MC] تم استلام طلب جديد.")
    auth_header = request.headers.get('Authorization')
    if not MANYCHAT_SECRET_KEY or auth_header != f'Bearer {MANYCHAT_SECRET_KEY}':
        logger.critical("🚨 [WEBHOOK-MC] محاولة وصول غير مصرح بها!")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    data = request.get_json()
    if not data:
        logger.error("❌ [WEBHOOK-MC] CRITICAL: لم يتم استلام بيانات JSON.")
        return jsonify({"status": "error", "message": "Request body must be JSON."}), 400
        
    full_contact = data.get("full_contact")
    if not full_contact:
        logger.error("❌ [WEBHOOK-MC] CRITICAL: 'full_contact' غير موجودة.")
        return jsonify({"status": "error", "message": "'full_contact' data is required."}), 400

    session = get_or_create_session_from_contact(full_contact, "ManyChat")
    if not session:
        logger.error("❌ [WEBHOOK-MC] فشل في إنشاء أو الحصول على جلسة.")
        return jsonify({"status": "error", "message": "Failed to create or get session"}), 500

    last_input = full_contact.get("last_text_input") or full_contact.get("last_input_text")
    if not last_input:
        logger.warning("[WEBHOOK-MC] لا يوجد إدخال نصي للمعالجة.")
        return jsonify({"status": "received", "message": "No text input to process"}), 200
    
    logger.info(f"💬 [WEBHOOK-MC] الإدخال المستلم: \"{last_input}\"")
    is_url = last_input.startswith(("http://", "https://" ))
    is_media_url = is_url and ("cdn.fbsbx.com" in last_input or "scontent" in last_input)

    def background_task():
        if is_media_url:
            logger.info("🖼️ [WEBHOOK-MC] تم اكتشاف رابط وسائط. بدء المعالجة في الخلفية.")
            media_content = download_media_from_url(last_input)
            if not media_content:
                send_manychat_reply(session["_id"], "⚠️ عفوًا، لم أتمكن من تحميل الملف الذي أرسلته.", platform=session["platform"])
                return

            is_audio = any(ext in last_input for ext in ['.mp4', '.mp3', '.ogg']) or "audioclip" in last_input
            if is_audio:
                transcribed_text = transcribe_audio(media_content, file_format="mp4")
                if transcribed_text:
                    content_for_assistant = f"[رسالة صوتية من العميل]: \"{transcribed_text}\""
                    add_to_processing_queue(session, content_for_assistant)
            else: # It's an image
                description = asyncio.run(get_image_description_for_assistant(base64.b64encode(media_content).decode('utf-8')))
                if description:
                    content_for_assistant = f"[وصف صورة أرسلها العميل]: {description}"
                    add_to_processing_queue(session, content_for_assistant)
        else:
            logger.info("📝 [WEBHOOK-MC] تم تحديد الإدخال كنص عادي.")
            add_to_processing_queue(session, last_input)

    threading.Thread(target=background_task).start()
    return jsonify({"status": "received"}), 200

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

    @app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
    async def telegram_webhook_handler():
        data = request.get_json()
        update = telegram.Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return jsonify({"status": "ok"})
    logger.info("✅ [TELEGRAM] تم إعداد معالجات تليجرام والويب هوك بنجاح.")

# --- نقطة الدخول الرئيسية ---
@app.route("/")
def home():
    return "✅ Bot is running with Detailed Vision Logic (v12 - Retry Patch)."

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل. يرجى استخدام خادم WSGI (مثل Gunicorn) لتشغيله في بيئة الإنتاج.")
