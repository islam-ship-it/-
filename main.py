# main.py (Patched v22 - Final)
# - Structured JSON payload to assistant (prevents "thanks for the file" and duplicate/garbled replies)
# - Uses last 10 messages as structured history (role + text)
# - Keeps threading/timer architecture; strict Lock per user to prevent concurrent processing
# - Audio transcribed to text (whisper-1) and included in audio_texts list
# - Images sent as URLs in images list
# - Batch window 0.5s: text+image+audio merged

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
pending_messages = {}
message_timers = {}
processing_locks = {}
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

# --- دوال OpenAI ---
async def get_assistant_reply(session, json_payload, timeout=90):
    """
    json_payload: a Python dict (structured), will be converted to JSON string and sent to the assistant.
    This function creates/uses a thread, appends a user message containing the JSON, then creates a Run and waits.
    """
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

    # Prepare structured JSON string for assistant
    # We include a short wrapper instruction to ensure the assistant:
    #  - reads the JSON and responds with a single message
    #  - does NOT comment on attachments or say 'thanks for the file'
    payload_string = json.dumps(json_payload, ensure_ascii=False)
    instruction = (
        "You are a helpful assistant. The user's input is provided below as a JSON object. "
        "Read the JSON and answer the user's request once, in Arabic. "
        "Do NOT mention or apologize about files/attachments. "
        "Do NOT output JSON — output only the natural-language reply to the user's request. "
        "Keep the reply concise and focused.\n\n"
        "JSON:\n"
    )
    enriched_content = instruction + payload_string

    try:
        # --- Wait if there is an active run ---
        runs = await asyncio.to_thread(client.beta.threads.runs.list, thread_id=thread_id, limit=1)
        if runs.data and runs.data[0].status in ["queued", "in_progress"]:
            active_run = runs.data[0]
            logger.warning(f"⏳ [ASSISTANT] تم العثور على Run نشط سابق ({active_run.id}). جاري الانتظار حتى يكتمل.")
            start_time = time.time()
            while active_run.status in ["queued", "in_progress"]:
                if time.time() - start_time > timeout:
                    logger.error(f"⏰ [ASSISTANT] Timeout waiting for active run ({active_run.id}).")
                    return "⚠️ حدث تأخير في الرد بسبب معالجة سابقة لم تكتمل."
                await asyncio.sleep(1)
                active_run = await asyncio.to_thread(client.beta.threads.runs.retrieve, thread_id=thread_id, run_id=active_run.id)

        # add the structured message as a single user message
        logger.info(f"💬 [ASSISTANT] إضافة رسالة مُهيكلة إلى Thread {thread_id} للمستخدم {user_id}.")
        await asyncio.to_thread(client.beta.threads.messages.create, thread_id=thread_id, role="user", content=enriched_content)

        # start a run
        logger.info(f"▶️ [ASSISTANT] بدء تشغيل المساعد (Run) على Thread {thread_id}.")
        run = await asyncio.to_thread(client.beta.threads.runs.create, thread_id=thread_id, assistant_id=ASSISTANT_ID_PREMIUM)

        # wait for completion
        start_time = time.time()
        while run.status in ["queued", "in_progress"]:
            if time.time() - start_time > timeout:
                logger.error(f"⏰ [ASSISTANT] Timeout! run {run.id} took more than {timeout} seconds.")
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

# --- إرسال رد ManyChat ---
def send_manychat_reply_async(subscriber_id, text_message, platform):
    logger.info(f"📤 [SENDER] بدء إرسال رد إلى {subscriber_id} على منصة {platform}...")
    if not MANYCHAT_API_KEY:
        logger.error("❌ [SENDER] مفتاح MANYCHAT_API_KEY غير موجود!")
        return

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform == "Instagram" else "facebook"

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message.strip()}] }},
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

# --- تفريغ الصوت من URL ---
def transcribe_audio_url(audio_url):
    try:
        logger.info(f"🔊 [TRANSCRIBE] تنزيل ملف الصوت من {audio_url} ...")
        resp = requests.get(audio_url, timeout=20)
        resp.raise_for_status()
        audio_bytes = resp.content
    except Exception as e:
        logger.error(f"❌ [TRANSCRIBE] فشل تنزيل الصوت من {audio_url}: {e}")
        return None

    try:
        transcription_resp = asyncio.run(asyncio.to_thread(
            client.audio.transcriptions.create, file=("audio.webm", audio_bytes), model="whisper-1"))
        
        if hasattr(transcription_resp, "text"):
            return transcription_resp.text
        if isinstance(transcription_resp, dict) and transcription_resp.get("text"):
            return transcription_resp.get("text")
        return str(transcription_resp)
    except Exception as e:
        logger.error(f"❌ [TRANSCRIBE] فشل تفريغ الصوت عبر OpenAI: {e}", exc_info=True)
        return None

# --- schedule_assistant_response (builds structured JSON payload) ---
def schedule_assistant_response(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    if not lock.acquire(blocking=False):
        logger.warning(f"⚠️ [PROCESSOR] تم تجاهل طلب معالجة للمستخدم {user_id} لأن معالجًا آخر لا يزال نشطًا (Lock acquired).")
        return

    try:
        if user_id not in pending_messages or not pending_messages[user_id]:
            return

        user_data = pending_messages[user_id]
        session = user_data["session"]

        texts = user_data.get("texts", [])
        images = user_data.get("images", [])
        audios = user_data.get("audios", [])

        # Collect final structured fields
        main_text = "\n".join(texts).strip() if texts else ""
        audio_texts = []
        for audio_url in audios:
            transcript = transcribe_audio_url(audio_url)
            if transcript:
                audio_texts.append(transcript)
            else:
                # If transcription failed, include a short placeholder without "file" wording
                audio_texts.append("(تعذر تحويل الصوت إلى نص)")

        images_list = images[:]  # copy

        # Build structured history: last 10 messages from thread if available
        history_struct = []
        thread_id = session.get("openai_thread_id")
        if thread_id:
            try:
                messages = asyncio.run(asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=10))
                # messages.data may be newest-first; reverse to chronological
                for msg in reversed(messages.data):
                    # Try to extract text safely; skip non-text system messages
                    try:
                        content_text = ""
                        if msg.content and len(msg.content) > 0:
                            # Heuristic: content[0].text.value if exists
                            c = msg.content[0]
                            if hasattr(c, "text") and getattr(c.text, "value", None):
                                content_text = c.text.value
                            elif isinstance(c, dict) and c.get("text"):
                                content_text = c.get("text")
                            else:
                                content_text = str(c)
                        history_struct.append({"role": msg.role, "text": content_text})
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"⚠️ [MEMORY] تعذر استرجاع history للمستخدم {user_id}: {e}")

        # Build final structured payload for assistant
        structured_payload = {
            "type": "multi_input",
            "text": main_text,
            "audio_texts": audio_texts,
            "images": images_list,
            "history": history_struct  # list of {role, text}
        }

        logger.info(f"⚙️ [PROCESSOR] إرسال payload مُنظّم للمساعد للمستخدم {user_id}: "
                    f"text_len={len(main_text)}, images={len(images_list)}, audio_texts={len(audio_texts)}, history={len(history_struct)}")

        # Call assistant and wait for reply synchronously
        reply_text = asyncio.run(get_assistant_reply(session, structured_payload))

        if reply_text:
            send_manychat_reply_async(user_id, reply_text, platform=session.get("platform", "Facebook"))

        # cleanup
        if user_id in pending_messages: del pending_messages[user_id]
        if user_id in message_timers: del message_timers[user_id]
        logger.info(f"🗑️ [PROCESSOR] تم الانتهاء من المعالجة للمستخدم {user_id}.")

    finally:
        lock.release()
        logger.info(f"🔓 [LOCK] تم تحرير القفل للمستخدم {user_id}.")

# --- إضافة إلى قائمة الانتظار (يدعم نص/صورة/صوت) ---
def add_to_processing_queue(session, payload):
    user_id = session["_id"]

    if user_id not in pending_messages or not pending_messages[user_id]:
        pending_messages[user_id] = {"texts": [], "images": [], "audios": [], "session": session}
    else:
        pending_messages[user_id]["session"] = session

    # cancel previous timer if exists
    if user_id in message_timers:
        try:
            message_timers[user_id].cancel()
            logger.info(f"⏳ [DEBOUNCE] تم إلغاء المؤقت القديم للمستخدم {user_id} لأنه أرسل رسالة جديدة.")
        except Exception:
            pass

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
        logger.warning(f"⚠️ [QUEUE] payload type unknown for user {user_id}: {type(payload)}")

    current_texts = pending_messages[user_id]['texts']
    current_images = pending_messages[user_id]['images']
    current_audios = pending_messages[user_id]['audios']
    if not (current_texts or current_images or current_audios):
        logger.warning(f"⚠️ [QUEUE] لا يوجد محتوى لإضافته للمستخدم {user_id}.")
        return

    logger.info(f"➕ [QUEUE] تمت إضافة محتوى إلى قائمة الانتظار للمستخدم {user_id}. counts: texts={len(current_texts)}, images={len(current_images)}, audios={len(current_audios)}")

    # start a new debounce timer
    timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[user_id])
    message_timers[user_id] = timer
    timer.start()
    logger.info(f"⏳ [DEBOUNCE] بدء مؤقت جديد لمدة {BATCH_WAIT_TIME} ثانية للمستخدم {user_id}.")

# --- ويب هوك ManyChat ---
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

    last_input = contact_data.get("last_text_input") or contact_data.get("last_input_text") or data.get("last_input")
    image_url = None
    audio_url = None

    att = contact_data.get("last_attachment") or contact_data.get("attachment") or data.get("last_attachment")
    if isinstance(att, dict):
        att_type = att.get("type")
        if att_type == "image":
            image_url = att.get("url") or att.get("file_url")
        elif att_type == "audio":
            audio_url = att.get("url") or att.get("file_url")

    if not image_url:
        attachments = contact_data.get("attachments") or data.get("attachments")
        if isinstance(attachments, list):
            for a in attachments:
                if a.get("type") == "image":
                    image_url = a.get("url") or a.get("file_url")
                    break

    if not audio_url:
        audio_url = contact_data.get("last_audio_url") or contact_data.get("audio_url") or data.get("last_audio_url")

    if not any([last_input, image_url, audio_url]):
        logger.warning("[WEBHOOK] لم يتم العثور على إدخال نصي/صورة/صوت للمعالجة.")
        return jsonify({"status": "no_input_received"})

    payload = {"text": last_input, "image_url": image_url, "audio_url": audio_url}
    add_to_processing_queue(session, payload)
    
    logger.info("✅ [WEBHOOK] تم إرسال الطلب للمعالجة. إرجاع تأكيد استلام فوري.")
    return jsonify({"status": "received"})

# --- نقطة الدخول الرئيسية ---
@app.route("/")
def home():
    return "✅ Bot is running in Unified Mode with Structured JSON (v22)."

if __name__ == "__main__":
    logger.info("🚀 التطبيق جاهز للتشغيل. يرجى استخدام خادم WSGI (مثل Gunicorn) لتشغيله في بيئة الإنتاج.")
