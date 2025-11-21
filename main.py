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

# ===========================
# 🔥 إعداد اللوجات — نظيفة بالعربي
# ===========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

logger.info("▶️ بدء التشغيل...")

# ===========================
# تحميل الإعدادات
# ===========================
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
MONGO_URI = os.getenv("MONGO_URI")

MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")


# ===========================
# قاعدة البيانات
# ===========================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ متصل بقاعدة البيانات")
except Exception as e:
    logger.error(f"❌ خطأ الاتصال بقاعدة البيانات: {e}")
    exit()


# ===========================
# إعداد Flask و OpenAI
# ===========================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("🚀 Flask و OpenAI جاهزين")


# ===========================
# نظام تجميع الرسائل (Batching)
# ===========================
pending_messages = {}
message_timers = {}
processing_locks = {}
queue_lock = threading.Lock()

BATCH_WAIT_TIME = 2.0  # ثانيتين بعد آخر رسالة


# ===========================
# إدارة السيشن
# ===========================
def get_or_create_session_from_contact(contact_data, platform):
    user_id = str(contact_data.get("id"))
    if not user_id:
        logger.error("❌ user_id غير موجود")
        return None

    session = sessions_collection.find_one({"_id": user_id})
    now_utc = datetime.now(timezone.utc)

    main_platform = "Instagram" if "instagram" in (contact_data.get("source","").lower()) else "Facebook"

    if session:
        sessions_collection.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact_date": now_utc,
                "platform": main_platform,
                "profile.name": contact_data.get("name"),
                "profile.profile_pic": contact_data.get("profile_pic"),
                "status": "active"
            }}
        )
        return sessions_collection.find_one({"_id": user_id})

    new_session = {
        "_id": user_id,
        "platform": main_platform,
        "profile": {
            "name": contact_data.get("name"),
            "first_name": contact_data.get("first_name"),
            "last_name": contact_data.get("last_name"),
            "profile_pic": contact_data.get("profile_pic"),
        },
        "openai_thread_id": None,
        "tags": [f"source:{main_platform.lower()}"],
        "custom_fields": contact_data.get("custom_fields", {}),
        "conversation_summary": "",
        "status": "active",
        "first_contact_date": now_utc,
        "last_contact_date": now_utc
    }
    sessions_collection.insert_one(new_session)
    return new_session


# ===========================
# Vision + Whisper
# ===========================
async def get_image_description_for_assistant(base64_image):
    logger.info("🖼️ معالجة صورة واردة...")
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4.1",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "اقرأ محتوى الصورة بدقة."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }],
            max_tokens=500
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ خطأ في معالجة الصورة: {e}")
        return None


def transcribe_audio(content, fmt="mp4"):
    filename = f"temp.{fmt}"
    with open(filename, "wb") as f:
        f.write(content)

    try:
        with open(filename, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)
        os.remove(filename)
        return tr.text
    except:
        os.remove(filename)
        return None


# ===========================
# إرسال الرد للعميل بطريقة صحيحة
# ===========================
def send_manychat_reply(subscriber_id, text_message, platform):
    logger.info(f"💬 إرسال رد للعميل {subscriber_id}")

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    channel = "instagram" if platform == "Instagram" else "facebook"

    # ❗ إرسال الرسالة كاملة بدون تقطيع
    msgs = [{"type": "text", "text": text_message}]

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"messages": msgs}},
        "channel": channel,
    }

    try:
        requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
    except Exception as e:
        logger.error(f"❌ خطأ ManyChat: {e}")


# ===========================
# تحميل وسائط
# ===========================
def download_media_from_url(url):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.error(f"❌ فشل تحميل الوسائط: {e}")
        return None


# ===========================
# تجميع الرسائل قبل الإرسال للمساعد
# ===========================
async def get_assistant_reply(session, content):
    thread_id = session.get("openai_thread_id")

    if not thread_id:
        thread = await asyncio.to_thread(client.beta.threads.create)
        thread_id = thread.id
        sessions_collection.update_one({"_id": session["_id"]}, {"$set": {"openai_thread_id": thread_id}})

    await asyncio.to_thread(client.beta.threads.messages.create,
                            thread_id=thread_id, role="user", content=content)

    run = await asyncio.to_thread(
        client.beta.threads.runs.create,
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID_PREMIUM
    )

    while run.status in ["queued", "in_progress"]:
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
        return messages.data[0].content[0].text.value.strip()

    return "⚠️ حدث خطأ أثناء معالجة الرسالة."


def schedule_assistant_response(user_id):
    with queue_lock:
        data = pending_messages.get(user_id)
        if not data:
            return

        session = data["session"]
        merged = "\n".join(data["texts"])

        logger.info(f"📦 دمج {len(data['texts'])} رسالة وإرسالها للمساعد")
        logger.info(f"📝 محتوى الطلب:\n{merged}")

        pending_messages.pop(user_id, None)
        message_timers.pop(user_id, None)

    reply = asyncio.run(get_assistant_reply(session, merged))
    send_manychat_reply(user_id, reply, session["platform"])
    logger.info("🤖 تم إرسال رد المساعد للعميل")


def add_to_queue(session, text):
    uid = session["_id"]

    with queue_lock:
        if uid not in pending_messages:
            pending_messages[uid] = {"texts": [], "session": session}

        pending_messages[uid]["texts"].append(text)

        logger.info(f"📩 استلام رسالة جديدة من {uid}: {text}")
        logger.info(f"📊 إجمالي الرسائل المنتظرة: {len(pending_messages[uid]['texts'])}")

        if uid in message_timers:
            message_timers[uid].cancel()

        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[uid])
        message_timers[uid] = timer
        timer.start()


# ===========================
# Webhook ManyChat
# ===========================
@app.route("/manychat_webhook", methods=["POST"])
def mc_webhook():
    data = request.get_json()

    contact = data.get("full_contact")
    if not contact:
        return jsonify({"error": "missing contact"}), 400

    session = get_or_create_session_from_contact(contact, "ManyChat")

    txt = contact.get("last_text_input") or contact.get("last_input_text")
    if not txt:
        return jsonify({"ok": True}), 200

    logger.info(f"📥 رسالة واردة: {txt}")

    is_url = txt.startswith("http")
    is_media = is_url and ("cdn.fbsbx.com" in txt or "scontent" in txt)

    def bg():
        if is_media:
            media = download_media_from_url(txt)
            if not media:
                send_manychat_reply(session["_id"], "لم أتمكن من تحميل الوسائط.", session["platform"])
                return

            if any(ext in txt for ext in [".mp3", ".mp4", ".ogg"]):
                tr = transcribe_audio(media)
                if tr:
                    add_to_queue(session, f"[رسالة صوتية]: {tr}")
            else:
                desc = asyncio.run(
                    get_image_description_for_assistant(base64.b64encode(media).decode())
                )
                if desc:
                    add_to_queue(session, f"[صورة]: {desc}")
        else:
            add_to_queue(session, txt)

    threading.Thread(target=bg).start()
    return jsonify({"ok": True}), 200


@app.route("/")
def home():
    return "Bot running."


if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز للعمل")
    app.run(host="0.0.0.0", port=5000)
