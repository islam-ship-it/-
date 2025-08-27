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

# --- متغيرات عالمية ---
pending_messages = {}
message_timers = {}
processing_locks = {}
BATCH_WAIT_TIME = 10.0

# --- دوال إدارة الجلسات ---
def get_or_create_session_from_contact(contact_data, platform):
    logger.debug(f"🔄 [SESSION] بدء عملية الحصول على جلسة أو إنشائها للمنصة: {platform}")
    user_id = None
    if platform in ["ManyChat-Instagram", "ManyChat-Facebook"]:
        user_id = str(contact_data.get("id"))
    elif platform == "Telegram":
        user_id = str(contact_data.get("id"))
    
    if not user_id:
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

# --- دوال OpenAI ---

async def get_image_description_from_openai(base64_image, caption=""):
    logger.info("🤖 [CHAT-VISION] بدء تحليل صورة باستخدام Chat Completions API (gpt-4o).")
    prompt_text = f"هذه صورة أرسلها عميل. التعليق عليها هو: '{caption}'. صفها له بشكل جذاب ومختصر باللغة العربية، ثم اسأله كيف يمكنك مساعدته بخصوصها."
    if not caption:
        prompt_text = "هذه صورة أرسلها عميل. صفها له بشكل جذاب ومختصر باللغة العربية، ثم اسأله كيف يمكنك مساعدته بخصوصها."

    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                    ],
                }
            ],
            max_tokens=300,
        )
        reply = response.choices[0].message.content
        logger.info(f"✅ [CHAT-VISION] تم تحليل الصورة بنجاح. الرد: \"{reply}\"")
        return reply
    except Exception as e:
        logger.error(f"❌ [CHAT-VISION] فشل تحليل الصورة: {e}", exc_info=True)
        return "⚠️ عفوًا، لم أتمكن من تحليل الصورة. هل يمكنك وصفها لي؟"

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

    if isinstance(content, str): content = [{"type": "text", "text": content}]
    logger.debug(f"💬 [ASSISTANT] المحتوى الذي سيتم إرساله إلى OpenAI: {json.dumps(content, ensure_ascii=False)}")

    try:
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

# --- آلية التجميع والمعالجة ---
def process_batched_messages_universal(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages or not pending_messages[user_id]:
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
    
    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session, **kwargs}
    
    pending_messages[user_id]["texts"].append(text)
    logger.info(f"➕ [HANDLER] تمت إضافة الرسالة إلى دفعة المعالجة. حجم الدفعة الآن: {len(pending_messages[user_id]['texts'])}")
    
    timer = threading.Timer(BATCH_WAIT_TIME, process_batched_messages_universal, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

def process_media_message_immediately(session, media_type, media_payload, **kwargs):
    async def async_target():
        user_id = session["_id"]
        platform = session["platform"]
        reply_text = None

        if media_type == "image":
            logger.info(f"⚙️ [MEDIA HANDLER] بدء معالجة فورية لـ 'صورة' للمستخدم {user_id}.")
            caption = kwargs.get("caption", "")
            reply_text = await get_image_description_from_openai(media_payload, caption)
        elif media_type == "audio":
            logger.info(f"⚙️ [MEDIA HANDLER] بدء معالجة فورية لـ 'صوت' للمستخدم {user_id}.")
            reply_text = await get_assistant_reply(session, media_payload)
        
        if reply_text:
            if platform in ["Instagram", "Facebook"]:
                send_manychat_reply(user_id, reply_text)
            elif platform == "Telegram":
                bot_instance = telegram_app.bot
                business_id = kwargs.get("business_id")
                await send_telegram_message(bot_instance, user_id, reply_text, business_id)
        logger.info(f"✅ [MEDIA HANDLER] انتهت المعالجة الفورية للوسائط للمستخدم {user_id}.")
    
    thread = threading.Thread(target=lambda: asyncio.run(async_target()))
    thread.start()
    logger.debug("[MEDIA HANDLER] تم بدء thread جديد للمعالجة الفورية.")

# --- ويب هوك ManyChat (النسخة النهائية والمعدلة) ---
@flask_app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    logger.info("📞 [WEBHOOK] تم استلام طلب جديد على ManyChat Webhook.")
    auth_header = request.headers.get('Authorization')
    if not MANYCHAT_SECRET_KEY or auth_header != f'Bearer {MANYCHAT_SECRET_KEY}':
        logger.critical(f"🚨 [WEBHOOK] محاولة وصول غير مصرح بها! 🚨")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    data = request.get_json()
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

    # +++ هذا هو التعديل المنطقي الرئيسي +++
    if is_media_url:
        logger.info(f"🖼️ [WEBHOOK] تم اكتشاف رابط وسائط. سيتم معالجته كوسائط فقط.")
        is_audio = any(ext in last_input for ext in ['.mp4', '.mp3', '.ogg']) or "audioclip" in last_input
        
        media_content = download_media_from_url(last_input)
        if not media_content:
            logger.error(f"❌ [WEBHOOK] فشل تحميل الوسائط من الرابط: {last_input}")
            send_manychat_reply(session["_id"], "⚠️ عفوًا، لم أتمكن من تحميل الملف الذي أرسلته. قد يكون الرابط منتهي الصلاحية.")
            return jsonify({"status": "error", "message": "Failed to download media"}), 200

        if is_audio:
            logger.info("🎤 [WEBHOOK] تم تحديد الوسائط كـ 'صوت'.")
            transcribed_text = transcribe_audio(media_content, file_format="mp4")
            if transcribed_text:
                payload = f"العميل أرسل رسالة صوتية، هذا هو نصها: \"{transcribed_text}\""
                process_media_message_immediately(session, "audio", payload)
        else:
            logger.info("📷 [WEBHOOK] تم تحديد الوسائط كـ 'صورة'.")
            base64_image = base64.b64encode(media_content).decode('utf-8')
            process_media_message_immediately(session, "image", base64_image)
    else:
        logger.info("📝 [WEBHOOK] تم تحديد الإدخال كـ 'نص'. جاري إرساله للمعالجة المجمعة...")
        handle_text_message(session, last_input)

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
        
        if message.text:
            handle_text_message(session, message.text, business_id=business_id)
        else:
            if message.voice:
                voice_file = await message.voice.get_file()
                voice_content = await voice_file.download_as_bytearray()
                transcribed_text = transcribe_audio(bytes(voice_content), file_format="ogg")
                if transcribed_text: 
                    payload = f"رسالة صوتية: {transcribed_text}"
                    process_media_message_immediately(session, "audio", payload, business_id=business_id)
            elif message.photo:
                caption = message.caption or ""
                photo_file = await message.photo[-1].get_file()
                photo_content = await photo_file.download_as_bytearray()
                base64_image = base64.b64encode(bytes(photo_content)).decode('utf-8')
                process_media_message_immediately(session, "image", base64_image, caption=caption, business_id=business_id)
            
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
    return "✅ Bot is running with Final Logic Fix (v6 - Fully Integrated)."

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل. يرجى استخدام خادم WSGI (مثل Gunicorn) لتشغيله في بيئة الإنتاج.")
