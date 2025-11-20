# main.py (Patched v21 - Final)
# - Keeps original threading/timer architecture intact
# - Adds support for: image URLs, audio URLs (transcribed to text)
# - If text+image+audio arrive within the batch window -> they are merged into one message to the assistant
# - Audio transcription uses OpenAI audio.transcriptions API (called in a thread); model is 'whisper-1'
# - Minimal, safe changes; all original code paths preserved
# - FIX: Implemented a strict threading.Lock in schedule_assistant_response to prevent concurrent processing for the same user.
# - FIX: Added active waiting logic in get_assistant_reply to handle existing active Runs (safety net).
# - UPDATE: Removed long-term memory (summarization) and replaced it with sending the last 10 messages as context.

import os
import time
import json
import requests
import threading
import asyncio
import logging
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv

# --- الإعدادات ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)
load_dotenv()
logger.info("▶️ [START] تم تحميل إعدادات البيئة.")

# --- مفاتيح API ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
MONGO_URI = os.getenv("MONGO_URI")
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

# --- متغيرات عالمية للمعالجة غير المتزامنة ---
# pending_messages[user_id] = {"texts": [], "images": [], "audios": [], "session": session}
pending_messages = {}
message_timers = {}
processing_locks = {}
# انتظر ثانيتين بعد آخر رسالة من المستخدم قبل معالجة الدفعة
BATCH_WAIT_TIME = 0.5

# --- دوال إدارة الجلسات ---
def get_or_create_session_from_contact(contact_data):
    user_id = str(contact_data.get("id"))
    if not user_id:
        logger.error(f"❌ [SESSION] لم يتم العثور على user_id في البيانات: {contact_data}")
        return None
        
    session = sessions_collection.find_one({"_id": user_id})
    now_utc = datetime.now(timezone.utc)
    
    main_platform = "Unknown"
    contact_source = contact_data.get("source", "").lower()
    if "instagram" in contact_source:
        main_platform = "Instagram"
    elif "facebook" in contact_source:
        main_platform = "Facebook"
    elif "ig_id" in contact_data and contact_data.get("ig_id"):
        main_platform = "Instagram"
    else:
        main_platform = "Facebook"

    logger.info(f"ℹ️ [SESSION] تم تحديد المنصة '{main_platform}' للمستخدم {user_id}.")

    if session:
        update_fields = {
            "last_contact_date": now_utc, "platform": main_platform,
            "profile.name": contact_data.get("name"), "profile.profile_pic": contact_data.get("profile_pic"),
            "status": "active"
        }
        sessions_collection.update_one({"_id": user_id}, {"$set": {k: v for k, v in update_fields.items() if v is not None}})
        logger.info(f"🔄 [SESSION] تم تحديث الجلسة الحالية للمستخدم {user_id}.")
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

# --- تم إلغاء دوال الذاكرة طويلة الأمد (التلخيص) بناءً على طلب المستخدم ---

# --- دوال OpenAI (مُعدّلة لتستخدم الذاكرة) ---
async def get_assistant_reply(session, content, timeout=90):
    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")
    # تم إلغاء الذاكرة الطويلة (التلخيص) بناءً على طلب المستخدم.
    # سيتم إرسال آخر 10 رسائل من المحادثة كـ "سياق" بدلاً من ذلك.
    # يتم استرجاع الرسائل لاحقًا عند الحاجة.
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

    enriched_content = content
    # استرجاع آخر 10 رسائل من المحادثة كذاكرة قصيرة المدى
    try:
        messages = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=10)
        # تصفية الرسائل وإعدادها كـ "سياق"
        # ملاحظة: يتم عكس الترتيب لأن API يعيد الأحدث أولاً، ونحن نريدها بالترتيب الزمني
        history = "\n".join([f"{msg.role}: {msg.content[0].text.value}" for msg in reversed(messages.data) if msg.role != "user"])
        
        if history:
            logger.info(f"🧠 [MEMORY] تم إعداد آخر 10 رسائل من المحادثة كذاكرة قصيرة المدى.")
            enriched_content = f"For your context, here is the history of our last 10 messages (excluding your last message): \n---\n{history}\n---\nNow, please respond to the user's new message(s): '{content}'"
        else:
            logger.info(f"🧠 [MEMORY] لا توجد ذاكرة سابقة للمستخدم.")
            enriched_content = content
            
    except Exception as e:
        logger.error(f"❌ [MEMORY] فشل في استرجاع سجل المحادثة: {e}", exc_info=True)
        enriched_content = content

    try:
        # --- FIX: Active Waiting for existing Run ---
        # 1. التحقق من وجود Run نشط والانتظار حتى يكتمل
        runs = await asyncio.to_thread(client.beta.threads.runs.list, thread_id=thread_id, limit=1)
        if runs.data and runs.data[0].status in ["queued", "in_progress"]:
            active_run = runs.data[0]
            logger.warning(f"⏳ [ASSISTANT] تم العثور على Run نشط سابق ({active_run.id}). جاري الانتظار حتى يكتمل.")
            
            start_time = time.time()
            while active_run.status in ["queued", "in_progress"]:
                if time.time() - start_time > timeout:
                    logger.error(f"⏰ [ASSISTANT] Timeout! فشل انتظار Run نشط سابق ({active_run.id}).")
                    # يمكن هنا محاولة إلغاء الـ Run النشط إذا لزم الأمر، لكن الأمان هو العودة برسالة خطأ
                    return "⚠️ حدث تأخير في الرد بسبب معالجة سابقة لم تكتمل."
                await asyncio.sleep(1)
                active_run = await asyncio.to_thread(client.beta.threads.runs.retrieve, thread_id=thread_id, run_id=active_run.id)
            
            if active_run.status != "completed":
                logger.error(f"❌ [ASSISTANT] الـ Run السابق فشل أو تم إلغاؤه. الحالة: {active_run.status}")
                # إذا فشل الـ Run السابق، يمكننا المتابعة وإضافة الرسالة الجديدة
                
        # 2. إضافة الرسالة الجديدة بأمان
        logger.info(f"💬 [ASSISTANT] إضافة رسالة إلى Thread {thread_id}: '{content}'")
        await asyncio.to_thread(client.beta.threads.messages.create, thread_id=thread_id, role="user", content=enriched_content)
        
        # 3. إنشاء Run جديد
        logger.info(f"▶️ [ASSISTANT] بدء تشغيل المساعد (Run) على Thread {thread_id}.")
        run = await asyncio.to_thread(client.beta.threads.runs.create, thread_id=thread_id, assistant_id=ASSISTANT_ID_PREMIUM)
        
        # 4. انتظار الـ Run الجديد
        start_time = time.time()
        while run.status in ["queued", "in_progress"]:
            if time.time() - start_time > timeout:
                logger.error(f"⏰ [ASSISTANT] Timeout! استغرق الـ run {run.id} أكثر من {timeout} ثانية.")
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
            return "⚠️ عفوًا، حدث خطأ فني."
    except Exception as e:
        logger.error(f"❌ [ASSISTANT] حدث استثناء غير متوقع: {e}", exc_info=True)
        return "⚠️ عفوًا، حدث خطأ غير متوقع."

# --- دوال المعالجة غير المتزامنة ---
def send_manychat_reply_async(subscriber_id, text_message, platform):
    logger.info(f"📤 [SENDER] بدء إرسال رد إلى {subscriber_id} على منصة {platform}...")
    if not MANYCHAT_API_KEY:
        logger.error("❌ [SENDER] مفتاح MANYCHAT_API_KEY غير موجود!")
        return

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform == "Instagram" else "facebook"

    payload = {
        "subscriber_id": str(subscriber_id  ),
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message.strip()}]}},
        "channel": channel,
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=20)
        response.raise_for_status()
        logger.info(f"✅ [SENDER] تم إرسال الرسالة بنجاح إلى {subscriber_id} عبر {channel}.")
    except requests.exceptions.HTTPError as e:
        error_text = e.response.text if e.response is not None else str(e)
        logger.error(f"❌ [SENDER] فشل إرسال الرسالة: {e}. تفاصيل الخطأ: {error_text}")
    except Exception as e:
        logger.error(f"❌ [SENDER] خطأ غير متوقع أثناء الإرسال: {e}", exc_info=True)

# --- New helper: transcribe audio from a public URL (option C: only transcribe audio; images send as URLs)
def transcribe_audio_url(audio_url):
    """
    Downloads the audio from the given URL (simple GET) and calls OpenAI transcription.
    Returns the transcript string or None on failure.
    """
    try:
        logger.info(f"🔊 [TRANSCRIBE] تنزيل ملف الصوت من {audio_url} ...")
        resp = requests.get(audio_url, timeout=20)
        resp.raise_for_status()
        audio_bytes = resp.content
    except Exception as e:
        logger.error(f"❌ [TRANSCRIBE] فشل تنزيل الصوت من {audio_url}: {e}")
        return None

    try:
        # Use to_thread to avoid blocking main thread if OpenAI client is blocking
        transcription_resp = asyncio.run(asyncio.to_thread(
            client.audio.transcriptions.create, file=("audio.webm", audio_bytes), model="whisper-1"))
        
        if hasattr(transcription_resp, "text"):
            return transcription_resp.text
        if isinstance(transcription_resp, dict) and transcription_resp.get("text"):
            return transcription_resp.get("text")
        # fallback to string
        return str(transcription_resp)
    except Exception as e:
        logger.error(f"❌ [TRANSCRIBE] فشل تفريغ الصوت عبر OpenAI: {e}", exc_info=True)
        return None

def schedule_assistant_response(user_id):
    # --- FIX: Use a strict Lock for the entire processing block ---
    lock = processing_locks.setdefault(user_id, threading.Lock())
    
    # محاولة الحصول على القفل. إذا كان هناك معالج آخر يعمل، يتم تجاهل هذا الطلب
    # هذا يضمن أن عملية معالجة واحدة فقط يمكن أن تعمل في أي وقت للمستخدم الواحد.
    if not lock.acquire(blocking=False):
        logger.warning(f"⚠️ [PROCESSOR] تم تجاهل طلب معالجة للمستخدم {user_id} لأن معالجًا آخر لا يزال نشطًا (Lock acquired).")
        return
        
    try:
        if user_id not in pending_messages or not pending_messages[user_id]:
            return
        
        user_data = pending_messages[user_id]
        session = user_data["session"]

        # --- جمع الأنواع المختلفة بدلاً من نص واحد ---
        texts = user_data.get("texts", [])
        images = user_data.get("images", [])
        audios = user_data.get("audios", [])

        # دمج كل النصوص المجمعة في نص واحد مع فواصل أسطر
        combined_parts = []
        if texts:
            combined_parts.append("\n".join(texts))

        # أضف روابط الصور (URLs) كسطر يمكن للمساعد أن يرجع إليه
        for img_url in images:
            combined_parts.append(f"[Image]: {img_url}")

        # تفريغ الأصوات: نعمل تحويل إلى نص ونضيفها
        for audio_url in audios:
            # يتم تفريغ الصوت هنا بشكل متزامن داخل هذا الثريد
            transcript = transcribe_audio_url(audio_url)
            if transcript:
                combined_parts.append(f"[Audio transcript from {audio_url}]: {transcript}")
            else:
                combined_parts.append(f"[Audio at {audio_url}]: (failed to transcribe)")

        combined_content = "\n\n".join(combined_parts).strip()
        logger.info(f"⚙️ [PROCESSOR] بدء معالجة المحتوى المجمع للمستخدم {user_id}: '{combined_content}'")

        # Call assistant (unchanged flow) by running the async function from sync
        reply_text = asyncio.run(get_assistant_reply(session, combined_content))
        
        if reply_text:
            send_manychat_reply_async(user_id, reply_text, platform=session.get("platform", "Facebook"))
            
            # تم إلغاء الذاكرة الطويلة (التلخيص) بناءً على طلب المستخدم.
            # لا حاجة لجدولة عملية تلخيص الذاكرة.

        # cleanup
        if user_id in pending_messages: del pending_messages[user_id]
        if user_id in message_timers: del message_timers[user_id]
        logger.info(f"🗑️ [PROCESSOR] تم الانتهاء من المعالجة للمستخدم {user_id}.")
        
    finally:
        # تحرير القفل بعد الانتهاء من المعالجة
        lock.release()
        logger.info(f"🔓 [LOCK] تم تحرير القفل للمستخدم {user_id}.")

# --- تعديل add_to_processing_queue لدعم النص + صور + صوت ---
def add_to_processing_queue(session, payload):
    """
    payload يمكن أن يكون:
      - نص string -> يضاف إلى texts
      - dict -> {'text': ..., 'image_url': ..., 'audio_url': ...}
    """
    user_id = session["_id"]

    # ensure pending structure exists
    if user_id not in pending_messages or not pending_messages[user_id]:
        pending_messages[user_id] = {"texts": [], "images": [], "audios": [], "session": session}
    else:
        # always update session reference (fresh)
        pending_messages[user_id]["session"] = session

    # cancel previous timer if exists
    if user_id in message_timers:
        try:
            message_timers[user_id].cancel()
            logger.info(f"⏳ [DEBOUNCE] تم إلغاء المؤقت القديم للمستخدم {user_id} لأنه أرسل رسالة جديدة.")
        except Exception:
            pass

    # accept both simple strings and dict payloads
    if isinstance(payload, str):
        pending_messages[user_id]["texts"].append(payload)
    elif isinstance(payload, dict):
        text = payload.get("text")
        image_url = payload.get("image_url")
        audio_url = payload.get("audio_url")
        if text:
            pending_messages[user_id]["texts"].append(text)
        if image_url:
            pending_messages[user_id]["images"].append(image_url)
        if audio_url:
            pending_messages[user_id]["audios"].append(audio_url)
    else:
        # unknown type, ignore
        logger.warning(f"⚠️ [QUEUE] payload type unknown for user {user_id}: {type(payload)}")

    # Check if there is any content to process
    current_texts = pending_messages[user_id]['texts']
    current_images = pending_messages[user_id]['images']
    current_audios = pending_messages[user_id]['audios']
    
    if not (current_texts or current_images or current_audios):
        logger.warning(f"⚠️ [QUEUE] لا يوجد محتوى لإضافته للمستخدم {user_id}.")
        return

    logger.info(f"➕ [QUEUE] تمت إضافة محتوى إلى قائمة الانتظار للمستخدم {user_id}. "
                f"counts: texts={len(current_texts)}, "
                f"images={len(current_images)}, audios={len(current_audios)}")

    # start a new debounce timer
    timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[user_id])
    message_timers[user_id] = timer
    timer.start()
    logger.info(f"⏳ [DEBOUNCE] بدء مؤقت جديد لمدة {BATCH_WAIT_TIME} ثانية للمستخدم {user_id}.")

# --- ويب هوك ManyChat (النسخة الموحدة) ---
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    logger.info("📞 [WEBHOOK] تم استلام طلب جديد.")
    auth_header = request.headers.get('Authorization')
    if not MANYCHAT_SECRET_KEY or auth_header != f'Bearer {MANYCHAT_SECRET_KEY}':
        logger.critical("🚨 [WEBHOOK] محاولة وصول غير مصرح بها!")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    data = request.get_json()
    if not data or not data.get("full_contact"):
        logger.error("❌ [WEBHOOK] CRITICAL: 'full_contact' غير موجودة.")
        return jsonify({"status": "error", "message": "Invalid data"}), 400

    session = get_or_create_session_from_contact(data["full_contact"])
    if not session:
        logger.error("❌ [WEBHOOK] فشل في إنشاء أو الحصول على جلسة.")
        return jsonify({"status": "error", "message": "Failed to create session"}), 500

    contact_data = data.get("full_contact", {})

    # --- extract text, image, audio (compatible with common ManyChat shapes) ---
    last_input = contact_data.get("last_text_input") or contact_data.get("last_input_text") or data.get("last_input")
    image_url = None
    audio_url = None

    # attachments variants
    att = contact_data.get("last_attachment") or contact_data.get("attachment") or data.get("last_attachment")
    if isinstance(att, dict):
        att_type = att.get("type")
        if att_type == "image":
            image_url = att.get("url") or att.get("file_url")
        elif att_type == "audio":
            audio_url = att.get("url") or att.get("file_url")

    # other fields that some ManyChat variants use
    if not image_url:
        attachments = contact_data.get("attachments") or data.get("attachments")
        if isinstance(attachments, list):
            for a in attachments:
                if a.get("type") == "image":
                    image_url = a.get("url") or a.get("file_url")
                    break

    if not audio_url:
        audio_url = contact_data.get("last_audio_url") or contact_data.get("audio_url") or data.get("last_audio_url")

    # If nothing found, respond no_input_received
    if not any([last_input, image_url, audio_url]):
        logger.warning("[WEBHOOK] لم يتم العثور على إدخال نصي/صورة/صوت للمعالجة.")
        return jsonify({"status": "no_input_received"})

    # Build payload dict and enqueue (we use dict to allow images/audio)
    payload = {"text": last_input, "image_url": image_url, "audio_url": audio_url}
    add_to_processing_queue(session, payload)
    
    logger.info("✅ [WEBHOOK] تم إرسال الطلب للمعالجة. إرجاع تأكيد استلام فوري.")
    return jsonify({"status": "received"})

# --- نقطة الدخول الرئيسية ---
@app.route("/")
def home():
    return "✅ Bot is running in Unified Mode with Short-Term Memory (v21 - Final)."

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل. يرجى استخدام خادم WSGI (مثل Gunicorn) لتشغيله في بيئة الإنتاج.")
