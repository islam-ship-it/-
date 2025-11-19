import os
import time
import threading
import logging
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from pymongo import MongoClient
from openai import OpenAI
import requests

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -------------------------
# Load env
# -------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")
WORKFLOW_VERSION = os.getenv("WORKFLOW_VERSION")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", 2.0))

# Check required vars
required = {
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "WORKFLOW_ID": WORKFLOW_ID,
    "WORKFLOW_VERSION": WORKFLOW_VERSION,
    "MONGO_URI": MONGO_URI,
    "MANYCHAT_API_KEY": MANYCHAT_API_KEY,
    "MANYCHAT_SECRET_KEY": MANYCHAT_SECRET_KEY
}
missing = [k for k, v in required.items() if not v]
if missing:
    logger.critical(f"Missing environment variables: {missing}")
    raise SystemExit(1)

# -------------------------
# MongoDB
# -------------------------
client_db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client_db.get_database("workflow_bot")
sessions_col = db.get_collection("sessions")
messages_col = db.get_collection("messages")

try:
    client_db.admin.command("ping")
    logger.info("Connected to MongoDB")
except:
    logger.exception("MongoDB connection failed")
    raise

# -------------------------
# OpenAI Client
# -------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# Flask App
# -------------------------
app = Flask(__name__)

# -------------------------
# Batching State
# -------------------------
pending_messages = {}
message_timers = {}
processing_locks = {}

# -------------------------
# Session Management
# -------------------------
def get_or_create_session(contact):
    user_id = str(contact.get("id"))

    now = datetime.now(timezone.utc)
    source = str(contact.get("source", "")).lower()
    platform = "Instagram" if "instagram" in source else "Facebook"

    doc = sessions_col.find_one({"_id": user_id})
    if doc:
        sessions_col.update_one({"_id": user_id}, {
            "$set": {
                "last_contact_date": now,
                "platform": platform,
                "profile.name": contact.get("name"),
                "profile.profile_pic": contact.get("profile_pic"),
            }
        })
        return sessions_col.find_one({"_id": user_id})

    new_doc = {
        "_id": user_id,
        "platform": platform,
        "profile": {
            "name": contact.get("name"),
            "profile_pic": contact.get("profile_pic"),
        },
        "created": now,
        "last_contact_date": now
    }
    sessions_col.insert_one(new_doc)
    return new_doc

# -------------------------
# Save Message
# -------------------------
def save_message(user_id, role, text):
    try:
        messages_col.insert_one({
            "user_id": user_id,
            "role": role,
            "text": text,
            "ts": datetime.utcnow()
        })
    except:
        logger.exception("Failed to save message")

# -------------------------
# ManyChat Sender
# -------------------------
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
            "content": {"messages": [{"type": "text", "text": text.strip()}]}
        },
        "channel": channel
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        logger.info(f"Sent ManyChat reply to {subscriber_id}")
    except:
        logger.exception("ManyChat send failed")

# -------------------------
# Call Workflow
# -------------------------
def call_workflow(text):
    try:
        run = client.workflows.runs.create(
            workflow_id=WORKFLOW_ID,
            version=WORKFLOW_VERSION,
            input={"user_message": text}
        )

        # Poll until completed
        while True:
            status = client.workflows.runs.get(
                workflow_id=WORKFLOW_ID,
                run_id=run.id
            )
            if status.status == "completed":
                break
            time.sleep(0.5)

        # Get messages
        messages = client.workflows.messages.list(
            workflow_id=WORKFLOW_ID,
            run_id=run.id
        )

        for msg in messages.data:
            if msg.role == "assistant":
                return msg.content[0].text.value

        return "لم أجد ردًا من الوكيل."

    except:
        logger.exception("Workflow call failed")
        return "⚠️ حدث خطأ أثناء تشغيل الوكيل."

# -------------------------
# Processing Batches
# -------------------------
def process_user(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())

    with lock:
        if user_id not in pending_messages:
            return

        data = pending_messages.pop(user_id)
        session = data["session"]
        texts = data["texts"]
        combined = "\n".join(texts).strip()

        save_message(user_id, "user", combined)

        reply = call_workflow(combined)

        save_message(user_id, "assistant", reply)
        send_manychat_reply(user_id, reply, session.get("platform"))

        if user_id in message_timers:
            try:
                message_timers[user_id].cancel()
            except:
                pass

# -------------------------
# Queue
# -------------------------
def add_to_queue(session_doc, text):
    user_id = session_doc["_id"]

    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session_doc}

    pending_messages[user_id]["texts"].append(text)

    if user_id in message_timers:
        try:
            message_timers[user_id].cancel()
        except:
            pass

    timer = threading.Timer(BATCH_WAIT_TIME, process_user, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

# -------------------------
# Webhook
# -------------------------
@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    auth = request.headers.get("Authorization")
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json()
    contact = data.get("full_contact")

    if not contact:
        return jsonify({"error": "invalid"}), 400

    session_doc = get_or_create_session(contact)

    last_input = contact.get("last_text_input") or contact.get("last_input_text")
    if not last_input:
        return jsonify({"status": "no_input"})

    add_to_queue(session_doc, last_input)
    return jsonify({"status": "received"})

# -------------------------
# Health
# -------------------------
@app.route("/")
def home():
    return "Workflow Bot Running"

# -------------------------
# Run Server
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting on port {port}")
    app.run(host="0.0.0.0", port=port)
