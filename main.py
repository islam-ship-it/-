# main.py (patched) — original code with safe media support (images URLs + audio transcription)
# - Keeps original threading/timer architecture intact
# - Adds support for: image URLs, audio URLs (transcribed to text)
# - If text+image+audio arrive within the batch window -> they are merged into one message to the assistant
# - Audio transcription uses OpenAI audio.transcriptions API (called in a thread); adjust model name if needed
# - Minimal, safe changes; all original code paths preserved

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

# --- متغيرات عالمية ---
pending_messages = {}
message_timers = {}
processing_locks = {}

BATCH_WAIT_TIME = 0.5

# ✓ أضفنا "Grace Period" هنا
GRACE_PERIOD = 1.0    # ← أهم تعديل (يمنع الرد على كل رسالة لوحدها)

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

# --- دوال الذاكرة ---
async def summarize_and_save_conversation(user_id, thread_id):
    logger.info(f"🧠 [MEMORY] بدء عملية تلخيص الذاكرة للمستخدم {user_id}.")
    try:
        messages = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=20)
        history = "\n".join([f"{msg.role}: {msg.content[0].text.value}" for msg in reversed(messages.data)])
        
        prompt = (
            "Please summarize the following conversation concisely..."
            f"\n\nConversation:\n{history}\n\nSummary:"
        )

        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4.1-mini",
            messages=[{"role": "system", "content": prompt}]
        )
        summary = response.choices[0].message.content.strip()
        
        sessions_collection.update_one({"_id": user_id}, {"$set": {"conversation_summary": summary}})
        logger.info(f"✅ [MEMORY] تم تلخيص وتحديث الذاكرة للمستخدم {user_id}.")
    except Exception as e:
        logger.error(f"❌ [MEMORY] فشل في تلخيص المحادثة للمستخدم {user_id}: {e}", exc_info=True)

# --- دوال الرد من OpenAI ---
async def get_assistant_reply(session, content, timeout=90):
    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")
    summary = session.get("conversation_summary", "")
    logger.info(f"🤖 [ASSISTANT] بدء عملية الحصول على رد للمستخدم {user_id}.")

    if not thread_id:
        logger.warning(f"🧵 [ASSISTANT] لا يوجد thread ... يتم إنشاء واحد جديد.")
        try:
            thread = await asyncio.to_thread(client.beta.threads.create)
            thread_id = thread.id
            sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
        except Exception as e:
            logger.error(f"❌ فشل في إنشاء thread: {e}")
            return "⚠️ حدث خطأ أثناء تهيئة المحادثة."

    enriched_content = (
        f"For your context, here is a summary: '{summary}'. Respond: '{content}'"
        if summary
        else content
    )

    try:
        await asyncio.to_thread(
            client.beta.threads.messages.create,
            thread_id=thread_id,
            role="user",
            content=enriched_content
        )

        run = await asyncio.to_thread(
            client.beta.threads.runs.create,
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID_PREMIUM
        )

        start_time = time.time()
        while run.status in ["queued", "in_progress"]:
            if time.time() - start_time > timeout:
                return "⚠️ تأخير في الرد."
            await asyncio.sleep(1)
            run = await asyncio.to_thread(
                client.beta.threads.runs.retrieve,
                thread_id=thread_id,
                run_id=run.id
            )

        if run.status == "completed":
            messages = await asyncio.to_thread(
                client.beta.threads.messages.list,
                thread_id=thread_id,
                limit=1
            )
            reply = messages.data[0].content[0].text.value.strip()
            return reply

        return "⚠️ خطأ فني."
    except Exception as e:
        return "⚠️ حدث خطأ غير متوقع."

# --- إرسال الرد لـ ManyChat ---
def send_manychat_reply_async(subscriber_id, text_message, platform):
    if not MANYCHAT_API_KEY:
        return

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform == "Instagram" else "facebook"

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message.strip()}]}},
        "channel": channel,
    }

    try:
        requests.post(url, headers=headers, data=json.dumps(payload), timeout=20)
    except:
        pass

# --- تفريغ الصوت ---
def transcribe_audio_url(audio_url):
    try:
        resp = requests.get(audio_url, timeout=20)
        resp.raise_for_status()
        audio_bytes = resp.content
    except:
        return None

    try:
        transcription_resp = asyncio.run(
            asyncio.to_thread(client.audio.transcriptions.create, audio_bytes, "audio.webm", {"model": "gpt-4o-mini-transcribe"})
        )
        if hasattr(transcription_resp, "text"):
            return transcription_resp.text
        return str(transcription_resp)
    except:
        return None

# --- تجميع الرسائل + Grace Period ---
def schedule_assistant_response(user_id):

    # ← ← ← أهم إضافة ← ← ←
    try:
        logger.info(f"⏳ [DEBOUNCE] Grace period {GRACE_PERIOD}s before processing user {user_id}")
        time.sleep(GRACE_PERIOD)
    except:
       .pass

    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages or not pending_messages[user_id]:
            return
        
        user_data = pending_messages[user_id]
        session = user_data["session"]

        texts = user_data.get("texts", [])
        images = user_data.get("images", [])
        audios = user_data.get("audios", [])

        combined_parts = []
        if texts:
            combined_parts.append("\n".join(texts))

        for img_url in images:
            combined_parts.append(f"[Image]: {img_url}")

        for audio_url in audios:
            t = transcribe_audio_url(audio_url)
            combined_parts.append(f"[Audio]: {t or '(failed)'}")

        combined_content = "\n\n".join(combined_parts).strip()

        reply_text = asyncio.run(get_assistant_reply(session, combined_content))
        
        if reply_text:
            send_manychat_reply_async(user_id, reply_text, platform=session.get("platform", "Facebook"))
            
            thread_id = session.get("openai_thread_id")
            if thread_id:
                threading.Thread(
                    target=lambda: asyncio.run(summarize_and_save_conversation(user_id, thread_id))
                ).start()

        pending_messages.pop(user_id, None)
        message_timers.pop(user_id, None)

# --- إضافة الرسائل للكيو ---
def add_to_processing_queue(session, payload):
    user_id = session["_id"]

    if user_id not in pending_messages or not pending_messages[user_id]:
        pending_messages[user_id] = {"texts": [], "images": [], "audios": [], "session": session}
    else:
        pending_messages[user_id]["session"] = session

    if user_id in message_timers:
        try:
            message_timers[user_id].cancel()
        except:
            pass

    if isinstance(payload, str):
        pending_messages[user_id]["texts"].append(payload)
    elif isinstance(payload, dict):
        if payload.get("text"):
            pending_messages[user_id]["texts"].append(payload["text"])
        if payload.get("image_url"):
            pending_messages[user_id]["images"].append(payload["image_url"])
        if payload.get("audio_url"):
            pending_messages[user_id]["audios"].append(payload["audio_url"])

    timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

# --- WEBHOOK ---
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    data = request.get_json()
    if not data or not data.get("full_contact"):
        return jsonify({"status": "error"}), 400

    session = get_or_create_session_from_contact(data["full_contact"])

    contact = data.get("full_contact")

    last_input = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or data.get("last_input")
    )

    image_url = None
    audio_url = None

    att = contact.get("last_attachment")
    if isinstance(att, dict):
        if att.get("type") == "image":
            image_url = att.get("url")
        elif att.get("type") == "audio":
            audio_url = att.get("url")

    payload = {"text": last_input, "image_url": image_url, "audio_url": audio_url}
    add_to_processing_queue(session, payload)

    return jsonify({"status": "received"})

# --- MAIN ---
@app.route("/")
def home():
    return "bot up"

if __name__ == "__main__":
    logger.info("⚡ running...")
