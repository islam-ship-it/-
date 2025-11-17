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

# ---------------------------------------------------------------
#  LOGGING
# ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
logger.info("▶️ [START] Environment Loaded.")

load_dotenv()

# ---------------------------------------------------------------
#  ENV VARS
# ---------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")
WORKFLOW_VERSION = os.getenv("WORKFLOW_VERSION", "1")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

# ---------------------------------------------------------------
#  DATABASE
# ---------------------------------------------------------------
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ [DB] Connected to MongoDB successfully.")
except Exception as e:
    logger.critical(f"❌ [DB] Failed to connect: {e}", exc_info=True)
    exit()

# ---------------------------------------------------------------
#  APP + OPENAI CLIENT
# ---------------------------------------------------------------
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------------------------------------------------------
#  MESSAGE QUEUE
# ---------------------------------------------------------------
pending_messages = {}
message_timers = {}
processing_locks = {}
BATCH_WAIT_TIME = 2.0

# ---------------------------------------------------------------
#  SESSION HANDLER
# ---------------------------------------------------------------
def get_or_create_session(contact_data):
    user_id = str(contact_data.get("id"))
    if not user_id:
        return None

    session = sessions_collection.find_one({"_id": user_id})
    now = datetime.now(timezone.utc)

    platform = "Instagram" if "instagram" in str(contact_data.get("source", "")).lower() else "Facebook"

    if session:
        sessions_collection.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact_date": now,
                "platform": platform,
                "profile.name": contact_data.get("name"),
                "profile.profile_pic": contact_data.get("profile_pic"),
            }}
        )
        return sessions_collection.find_one({"_id": user_id})

    new_session = {
        "_id": user_id,
        "platform": platform,
        "profile": {
            "name": contact_data.get("name"),
            "profile_pic": contact_data.get("profile_pic"),
        },
        "created": now,
        "last_contact_date": now,
    }

    sessions_collection.insert_one(new_session)
    return new_session

# ---------------------------------------------------------------
#  SEND MANYCHAT MESSAGE
# ---------------------------------------------------------------
def send_manychat_reply(subscriber_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    channel = "instagram" if platform == "Instagram" else "facebook"

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {
            "version": "v2",
            "content": {
                "messages": [{"type": "text", "text": text.strip()}]
            }
        },
        "channel": channel
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        r.raise_for_status()
        logger.info(f"📤 [SEND] Message delivered → {subscriber_id}")
    except Exception as e:
        logger.error(f"❌ [SEND] Failed: {e}")

# ---------------------------------------------------------------
#  OPENAI WORKFLOW EXECUTION
# ---------------------------------------------------------------
async def run_agent_workflow(text, session):
    try:
        response = client.responses.create(
            model="gpt-4.1",
            input=text,
            agent={"workflow": WORKFLOW_ID, "version": WORKFLOW_VERSION}
        )
        return response.output_text
    except Exception as e:
        logger.error(f"❌ [AGENT] Error: {e}")
        return "⚠️ حدث خطأ أثناء معالجة طلبك."

# ---------------------------------------------------------------
#  PROCESSING QUEUE
# ---------------------------------------------------------------
def schedule_message_processing(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages:
            return

        data = pending_messages[user_id]
        session = data["session"]

        combined = "\n".join(data["texts"])
        logger.info(f"🔍 [PROCESS] Combined text: {combined}")

        reply = asyncio.run(run_agent_workflow(combined, session))

        send_manychat_reply(user_id, reply, session["platform"])

        del pending_messages[user_id]
        if user_id in message_timers:
            del message_timers[user_id]

def add_to_queue(session, text):
    user_id = session["_id"]

    if user_id in message_timers:
        message_timers[user_id].cancel()

    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session}

    pending_messages[user_id]["texts"].append(text)

    timer = threading.Timer(BATCH_WAIT_TIME, schedule_message_processing, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

# ---------------------------------------------------------------
#  MANYCHAT WEBHOOK
# ---------------------------------------------------------------
@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    auth = request.headers.get("Authorization")

    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact", {})

    session = get_or_create_session(contact)
    if not session:
        return jsonify({"error": "session-failed"}), 500

    last_input = (
        contact.get("last_text_input") or
        contact.get("last_input_text") or
        data.get("last_input")
    )

    if not last_input:
        return jsonify({"status": "no_input"})

    add_to_queue(session, last_input)

    return jsonify({"status": "received"})

# ---------------------------------------------------------------
@app.route("/")
def home():
    return "🚀 Bot Running — Render Version"

# ---------------------------------------------------------------
#  THE FIX FOR RENDER PORT  🔥
# ---------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
