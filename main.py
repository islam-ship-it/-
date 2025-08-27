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
# تغيير مستوى اللوق إلى DEBUG لإظهار كل الرسائل
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
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

# --- متغيرات عالمية لتجميع الرسائل ---
pending_messages = {}
message_timers = {}
processing_locks = {}
BATCH_WAIT_TIME = 10.0
logger.info(f"🕒 [CONFIG] تم تعيين مدة انتظار تجميع الرسائل إلى: {BATCH_WAIT_TIME} ثانية.")

# --- دوال إدارة الجلسات ---
def get_or_create_session_from_contact(contact_data, platform):
    logger.debug(f"🔄 [SESSION] بدء عملية الحصول على جلسة أو إنشائها للمنصة: {platform}")
    if platform in ["ManyChat-Instagram", "ManyChat-Facebook"]:
        user_id = str(contact_data.get("id"))
    elif platform == "Telegram":
        user_id = str(contact_data.get("id"))
    else:
        logger.error(f"❌ [SESSION] لم يتم تحديد user_id للمنصة {platform}. البيانات الواردة: {contact_data}")
        return None
    
    logger.info(f"🆔 [SESSION] معرّف المستخدم المحدد: {user_id}")
    session = sessions_collection.find_one({"_id": user_id})
    now_utc = datetime.now(timezone.utc)

    main_platform = "Unknown"
    if platform == "ManyChat-Instagram": main_platform = "Instagram"
    elif platform == "ManyChat-Facebook": main_platform = "Facebook"
    elif platform == "Telegram": main_platform = "Telegram"

    if session:
        logger.info(f"👤 [SESSION] تم العثور على مستخدم حالي. جاري تحديث بياناته...")
        update_fields = {
            "last_contact_date": now_utc,
            "platform": main_platform,
            "profile.name": contact_data.get("name"),
            "profile.profile_pic": contact_data.get("profile_pic"),
            "status": "active"
        }
        update_fields = {k: v for k, v in update_fields.items() if v is not None}
        sessions_collection.update_one({"_id": user_id}, {"$set": update_fields})
        logger.info(f"✅ [SESSION] تم تحديث الجلسة للمستخدم: {user_id} على المنصة {platform}")
        session = sessions_collection.find_one({"_id": user_id})
    else:
        logger.info(f"🆕 [SESSION] مستخدم جديد. جاري إنشاء جلسة شاملة له: {user_id} على المنصة {platform}")
        new_session = {
            "_id": user_id, "platform": main_platform,
            "profile": {
                "name": contact_data.get("name"), "first_name": contact_data.get("first_name"),
                "last_name": contact_data.get("last_name"), "profile_pic": contact_data.get("profile_pic")
            },
            "openai_thread_id": None, "tags": [f"source:{main_platform.lower()}"],
            "custom_fields": contact_data.get("custom_fields", {}),
            "conversation_summary": "", "status": "active",
            "first_contact_date": now_utc, "last_contact_date": now_utc
        }
        sessions_collection.insert_one(new_session)
        session = new_session
        logger.info(f"✅ [SESSION] تم إنشاء جلسة جديدة بنجاح للمستخدم {user_id}")

    return session

async def get_assistant_reply(session, content):
    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")
    logger.info(f"🤖 [ASSISTANT] بدء عملية الحصول على رد للمستخدم {user_id}.")

    if not thread_id:
        logger.warning(f"🧵 [ASSISTANT] لا يوجد thread للمستخدم {user_id}. سيتم إنشاء واحد جديد.")
        try:
            thread = client.beta.threads.create()
            thread_id = thread.id
            sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
            logger.info(f"✅ [ASSISTANT] تم إنشاء وتخزين thread جديد: {thread_id}")
        except Exception as e:
            logger.error(f"❌ [ASSISTANT] فشل في إنشاء thread جديد: {e}", exc_info=True)
            return "⚠️ عفوًا، حدث خطأ أثناء تهيئة المحادثة."

    # تحويل المحتوى النصي إلى الصيغة المطلوبة
    if isinstance(content, str): content = [{"type": "text", "text": content}]
    logger.debug(f"💬 [ASSISTANT] المحتوى الذي سيتم إرساله إلى OpenAI: {json.dumps(content, ensure_ascii=False)}")

    try:
        logger.info(f"➕ [ASSISTANT] إضافة رسالة المستخدم إلى الـ thread: {thread_id}")
        client.beta.threads.messages.create(thread_id=thread_id, role="user", content=content)
        
        logger.info(f"▶️ [ASSISTANT] بدء تشغيل المساعد (run) للـ thread: {thread_id}")
        run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=ASSISTANT_ID_PREMIUM)
        
        start_time = time.time()
        while run.status in ["queued", "in_progress"]:
            if time.time() - start_time > 90:
                logger.error(f"⏰ [ASSISTANT] Timeout! استغرق الـ run {run.id} أكثر من 90 ثانية.")
                return "⚠️ حدث تأخير في الرد، يرجى المحاولة مرة أخرى."
            await asyncio.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            logger.debug(f"⏳ [ASSISTANT] حالة الـ run {run.id} هي: {run.status}")

        if run.status == "completed":
            logger.info(f"✅ [ASSISTANT] اكتمل الـ run بنجاح. جاري استرداد الرد...")
            messages = client.beta.threads.messages.list(thread_id=thread_id, limit=1)
            reply = messages.data[0].content[0].text.value.strip()
            logger.info(f"🗣️ [ASSISTANT] الرد الذي تم الحصول عليه: \"{reply}\"")
            return reply
        else:
            logger.error(f"❌ [ASSISTANT] لم يكتمل الـ run. الحالة: {run.status}. الخطأ: {run.last_error}")
            return "⚠️ عفوًا، حدث خطأ فني. فريقنا يعمل على إصلاحه."
    except Exception as e:
        logger.error(f"❌ [ASSISTANT] حدث استثناء غير متوقع أثناء التواصل مع OpenAI: {e}", exc_info=True)
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
    logger.debug(f"📦 [MANYCHAT] الحمولة (Payload) المرسلة: {json.dumps(payload)}")
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

def download_media_from_url(media_url, headers=None):
    logger.info(f"⬇️ [MEDIA] محاولة تحميل وسائط من الرابط: {media_url}")
    try:
        media_response = requests.get(media_url, headers=headers, timeout=20)
        media_response.raise_for_status()
        logger.info(f"✅ [MEDIA] تم تحميل الوسائط بنجاح. الحجم: {len(media_response.content)} بايت.")
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
        logger.info(f"✅ [WHISPER] تم تحويل الصوت إلى نص بنجاح: \"{transcription.text}\"")
        return transcription.text
    except Exception as e:
        logger.error(f"❌ [WHISPER] خطأ أثناء تحويل الصوت: {e}", exc_info=True)
        return None

# --- آلية التجميع والمعالجة الموحدة ---
def process_batched_messages_universal(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages or not pending_messages[user_id]:
            logger.warning(f"⚠️ [BATCH] تم استدعاء المعالجة للمستخدم {user_id} ولكن لا توجد رسائل معلقة.")
            return
        
        user_data = pending_messages[user_id]
        session = user_data["session"]
        platform = session["platform"]
        combined_content = "\n".join(user_data["texts"])
        
        logger.info(f"⚙️ [BATCH] بدء معالجة الرسائل المجمعة للمستخدم {user_id} على {platform}. المحتوى: '{combined_content}'")
        reply_text = asyncio.run(get_assistant_reply(session, combined_content))
        
        if reply_text:
            if platform in ["Instagram", "Facebook"]:
                send_manychat_reply(user_id, reply_text)
            elif platform == "Telegram":
                bot_instance = telegram_app.bot
                business_id = user_data.get("business_id")
                asyncio.run(send_telegram_message(bot_instance, user_id, reply_text, business_id))
        
        del pending_messages[user_id]
        if user_id in message_timers:
            del message_timers[user_id]
        logger.info(f"🗑️ [BATCH] تم الانتهاء من المعالجة وحذف الرسائل المعلقة للمستخدم {user_id}.")

def handle_text_message(session, text, **kwargs):
    user_id = session["_id"]
    logger.info(f"📥 [HANDLER] استلام رسالة نصية من {user_id} على {session['platform']}.")
    if user_id in message_timers:
        message_timers[user_id].cancel()
        logger.debug(f"🔄 [HANDLER] تم إلغاء المؤقت القديم للمستخدم {user_id}.")
    
    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session, **kwargs}
    
    pending_messages[user_id]["texts"].append(text)
    logger.info(f"➕ [HANDLER] تمت إضافة الرسالة إلى دفعة المعالجة. حجم الدفعة الآن: {len(pending_messages[user_id]['texts'])}")
    
    timer = threading.Timer(BATCH_WAIT_TIME, process_batched_messages_universal, args=[user_id])
    message_timers[user_id] = timer
    timer.start()
    logger.debug(f"⏳ [HANDLER] تم بدء مؤقت جديد لمدة {BATCH_WAIT_TIME} ثانية للمستخدم {user_id}.")

def process_media_message_immediately(session, content_for_assistant, **kwargs):
    def target():
        user_id = session["_id"]
        platform = session["platform"]
        logger.info(f"⚙️ [MEDIA HANDLER] بدء معالجة فورية لرسالة وسائط للمستخدم {user_id} على {platform}.")
        reply_text = asyncio.run(get_assistant_reply(session, content_for_assistant))
        
        if reply_text:
            if platform in ["Instagram", "Facebook"]:
                send_manychat_reply(user_id, reply_text)
            elif platform == "Telegram":
                bot_instance = telegram_app.bot
                business_id = kwargs.get("business_id")
                asyncio.run(send_telegram_message(bot_instance, user_id, reply_text, business_id))
        logger.info(f"✅ [MEDIA HANDLER] انتهت المعالجة الفورية للوسائط للمستخدم {user_id}.")
    
    thread = threading.Thread(target=target)
    thread.start()
    logger.debug("[MEDIA HANDLER] تم بدء thread جديد للمعالجة الفورية.")

# --- ويب هوك ManyChat ---
@flask_app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    logger.info("📞 [WEBHOOK] تم استلام طلب جديد على ManyChat Webhook.")
    auth_header = request.headers.get('Authorization')
    if not MANYCHAT_SECRET_KEY or auth_header != f'Bearer {MANYCHAT_SECRET_KEY}':
        logger.critical(f"🚨 [WEBHOOK] محاولة وصول غير مصرح بها! 🚨")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    data = request.get_json()
    logger.debug(f"📄 [WEBHOOK] البيانات المستلمة من ManyChat: {json.dumps(data)}")
    full_contact = data.get("full_contact")
    
    if not full_contact:
        logger.error(f"❌ [WEBHOOK] CRITICAL: 'full_contact' غير موجودة في البيانات.")
        return jsonify({"status": "error", "message": "'full_contact' data is required."}), 400

    platform = "ManyChat-Instagram" if full_contact.get("ig_id") else "ManyChat-Facebook"
    session = get_or_create_session_from_contact(full_contact, platform)
    
    if not session:
        logger.error("❌ [WEBHOOK] فشل في إنشاء أو الحصول على جلسة.")
        return jsonify({"status": "error", "message": "Failed to create or get session"}), 500

    last_input = full_contact.get("last_text_input") or full_contact.get("last_input_text")
    if not last_input:
        logger.warning("[WEBHOOK] لا يوجد إدخال نصي للمعالجة (last_input).")
        return jsonify({"status": "received", "message": "No text input to process"}), 200
    
    logger.info(f"💬 [WEBHOOK] الإدخال المستلم: \"{last_input}\"")
    is_url = last_input.startswith(("http://", "https://" ))
    is_media_url = is_url and (any(ext in last_input for ext in ['.jpg', '.jpeg', '.png', '.gif', '.mp4', '.mp3', '.ogg']) or "cdn.fbsbx.com" in last_input or "scontent" in last_input)

    if is_media_url:
        logger.info(f"🖼️ [WEBHOOK] تم اكتشاف رابط وسائط: {last_input}")
        media_content = download_media_from_url(last_input)
        if media_content:
            logger.info("✅ [WEBHOOK] تم تحميل الوسائط بنجاح. جاري تحديد النوع...")
            content_for_assistant = None
            is_audio = any(ext in last_input for ext in ['.mp4', '.mp3', '.ogg']) or "audioclip" in last_input
            if is_audio:
                logger.info("🎤 [WEBHOOK] تم تحديد الوسائط كـ 'صوت'.")
                transcribed_text = transcribe_audio(media_content, file_format="mp4")
                if transcribed_text:
                    content_for_assistant = f"العميل أرسل رسالة صوتية، هذا هو نصها: \"{transcribed_text}\""
            else:
                logger.info("📷 [WEBHOOK] تم تحديد الوسائط كـ 'صورة'.")
                base64_image = base64.b64encode(media_content).decode('utf-8')
                content_for_assistant = [{"type": "text", "text": "صف هذه الصورة باختصار شديد باللغة العربية."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]
            
            if content_for_assistant:
                logger.info("🚀 [WEBHOOK] جاري إرسال محتوى الوسائط إلى المساعد للمعالجة الفورية...")
                process_media_message_immediately(session, content_for_assistant)
            else:
                logger.error("❌ [WEBHOOK] فشل في إنشاء محتوى للمساعد بعد معالجة الوسائط.")
        else:
            logger.error(f"❌ [WEBHOOK] فشل تحميل الوسائط من الرابط: {last_input}")
    else:
        logger.info("📝 [WEBHOOK] تم تحديد الإدخال كـ 'نص'. جاري إرساله للمعالجة المجمعة...")
        handle_text_message(session, last_input)

    return jsonify({"status": "received"}), 200

# --- منطق تيليجرام ---
if TELEGRAM_BOT_TOKEN:
    logger.info("🔌 [TELEGRAM] تم العثور على توكن تليجرام. جاري إعداد البوت...")
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    async def start_command(update, context):
        logger.info(f"▶️ [TELEGRAM] استلام أمر /start من المستخدم {update.effective_user.id}")
        await update.message.reply_text(f"أهلاً {update.effective_user.first_name}!")

    async def handle_telegram_message(update, context):
        logger.info("📞 [TELEGRAM] تم استلام رسالة جديدة على تليجرام.")
        message = update.message or update.business_message
        if not message: 
            logger.warning("[TELEGRAM] الرسالة فارغة، تم التجاهل.")
            return
        
        user_contact_data = {
            "id": message.from_user.id, "name": message.from_user.full_name,
            "first_name": message.from_user.first_name, "last_name": message.from_user.last_name,
        }
        session = get_or_create_session_from_contact(user_contact_data, "Telegram")
        if not session: return

        business_id = getattr(update.business_message, "business_connection_id", None) if hasattr(update, "business_message") and update.business_message else None
        
        if message.text:
            logger.info("📝 [TELEGRAM] الرسالة نصية.")
            handle_text_message(session, message.text, business_id=business_id)
        else:
            logger.info("🖼️/🎤 [TELEGRAM] الرسالة تحتوي على وسائط.")
            content_for_assistant = None
            if message.voice:
                logger.info("🎤 [TELEGRAM] الرسالة صوتية (voice).")
                voice_file = await message.voice.get_file()
                voice_content = await voice_file.download_as_bytearray()
                transcribed_text = transcribe_audio(bytes(voice_content), file_format="ogg")
                if transcribed_text: content_for_assistant = f"رسالة صوتية: {transcribed_text}"
            elif message.photo:
                logger.info("📷 [TELEGRAM] الرسالة صورة (photo).")
                caption = message.caption or ""
                photo_file = await message.photo[-1].get_file()
                photo_content = await photo_file.download_as_bytearray()
                base64_image = base64.b64encode(bytes(photo_content)).decode('utf-8')
                content_for_assistant = [{"type": "text", "text": f"هذه صورة أرسلها العميل. التعليق عليها هو: '{caption}'. قم بوصف الصورة والرد على التعليق."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]
            
            if content_for_assistant:
                logger.info("🚀 [TELEGRAM] جاري إرسال محتوى الوسائط للمعالجة الفورية...")
                process_media_message_immediately(session, content_for_assistant, business_id=business_id)
            else:
                logger.warning("[TELEGRAM] تم استلام وسائط ولكن لم يتم إنشاء محتوى للمساعد (قد تكون نوع غير مدعوم).")

    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_telegram_message))

    @flask_app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
    async def telegram_webhook_handler():
        logger.info("📞 [TELEGRAM WEBHOOK] تم استلام طلب على ويب هوك تليجرام.")
        data = request.get_json()
        update = telegram.Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return jsonify({"status": "ok"})
    logger.info("✅ [TELEGRAM] تم إعداد معالجات تليجرام والويب هوك بنجاح.")

# --- الإعداد والتشغيل ---
@flask_app.route("/")
def home():
    return "✅ Bot is running with Advanced MongoDB Logging (v2 - Patched with Full Debug Logging)."

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل. يرجى استخدام خادم WSGI (مثل Gunicorn) لتشغيله في بيئة الإنتاج.")
    # للتشغيل المحلي للاختبار فقط، يمكنك إلغاء التعليق على السطر التالي:
    # flask_app.run(port=5000, debug=True)

