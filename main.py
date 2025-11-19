import os
import time
import json
import threading
import logging
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from pymongo import MongoClient
from openai import OpenAI
import requests

# ------------------------------------------------
# Logging
# ------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

# ------------------------------------------------
# ENV
# ------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")         # ← wf_xxxxxx
WORKFLOW_VERSION = os.getenv("WORKFLOW_VERSION")   # ← "5"
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
BATCH_WAIT = float(os.getenv("BATCH_WAIT_TIME", 2.0))

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
    logger.critical(f"Missing env vars: {missing}")
    raise SystemExit(1)

# ------------------------------------------------
# MongoDB
# ------------------------------------------------
mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo_client.get_database("multi_platform_bot")
sessions_col = db.sessions
messages_col = db.messages

try:
    mongo_client.admin.command("ping")
    logger.info("Connected to MongoDB")
except Exception:
    logger.exception("Mongo connection failed")
    raise

# ------------------------------------------------
# OpenAI Client
# ------------------------------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------------------------------------
# Flask
# ------------------------------------------------
app = Flask(__name__)

# ------------------------------------------------
# Batching buffers
# ------------------------------------------------
pending = {}        # user_id → {texts: [...], session}
timers = {}
locks = {}

# ------------------------------------------------
# Helpers
# ------------------------------------------------
def get_or_create_session(contact):
    user_id = str(contact["id"])
    now = datetime.now(timezone.utc)

    doc = sessions_col.find_one({"_id": user_id})
    platform = "Instagram" if "instagram" in str(contact.get("source", "")).lower() else "Facebook"

    if doc:
        sessions_col.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact": now,
                "platform": platform,
                "profile.name": contact.get("name"),
                "profile.pic": contact.get("profile_pic"),
            }}
        )
        return sessions_col.find_one({"_id": user_id})

    new_doc = {
        "_id": user_id,
        "platform": platform,
        "profile": {
            "name": contact.get("name"),
            "pic": contact.get("profile_pic")
        },
        "thread_id": None,
        "created": now,
        "last_contact": now,
    }
    sessions_col.insert_one(new_doc)
    return new_doc


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


# ------------------------------------------------
# ManyChat Send
# ------------------------------------------------
def send_manychat_reply(user_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    channel = "instagram" if platform == "Instagram" else "facebook"

    payload = {
        "subscriber_id": str(user_id),
        "data": {
            "version": "v2",
            "content": {
                "messages": [
                    {"type": "text", "text": text}
                ]
            }
        },
        "channel": channel
    }

    try:
        r = requests.post(url, json=payload, headers=headers)
        r.raise_for_status()
        logger.info(f"Reply sent to {user_id}")
    except:
        logger.exception("ManyChat send failed")


# ------------------------------------------------
# Workflow Engine (Threads + Runs)
# ------------------------------------------------
def ensure_thread(session_doc):
    """
    Creates an OpenAI thread once per user.
    """
    if session_doc.get("thread_id"):
        return session_doc["thread_id"]

    thread = client.threads.create()
    thread_id = thread.id

    sessions_col.update_one(
        {"_id": session_doc["_id"]},
        {"$set": {"thread_id": thread_id}}
    )
    return thread_id


def run_workflow(thread_id, text):
    """
    Executes OpenAI Workflow with Tools (File Search)
    """

    # Add message
    client.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=text
    )

    # Run workflow
    run = client.threads.runs.create(
        thread_id=thread_id,
        workflow_id=WORKFLOW_ID,
        version=WORKFLOW_VERSION
    )

    # Poll for completion
    while True:
        state = client.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id
        )
        if state.status == "completed":
            break
        time.sleep(0.4)

    # Fetch assistant reply
    msgs = client.threads.messages.list(thread_id=thread_id)
    for msg in reversed(msgs.data):
        if msg.role == "assistant":
            try:
                return msg.content[0].text.value
            except:
                return str(msg)

    return "⚠️ مشكلة في استخراج الرد"


# ------------------------------------------------
# Batching
# ------------------------------------------------
def process_user(user_id):
    lock = locks.setdefault(user_id, threading.Lock())

    with lock:
        data = pending.get(user_id)
        if not data:
            return

        session = data["session"]
        texts = data["texts"]

        combined = "\n".join(texts)

        save_message(user_id, "user", combined)

        thread_id = ensure_thread(session)
        reply = run_workflow(thread_id, combined)

        save_message(user_id, "assistant", reply)
        send_manychat_reply(user_id, reply, session["platform"])

        pending.pop(user_id, None)
        timers.pop(user_id, None)


def add_to_batch(session_doc, msg):
    uid = session_doc["_id"]

    if uid in timers:
        timers[uid].cancel()

    if uid not in pending:
        pending[uid] = {"texts": [], "session": session_doc}

    pending[uid]["texts"].append(msg)

    t = threading.Timer(BATCH_WAIT, process_user, args=[uid])
    timers[uid] = t
    t.start()


# ------------------------------------------------
# ManyChat Webhook
# ------------------------------------------------
@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    auth = request.headers.get("Authorization")
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact")

    if not contact:
        return jsonify({"error": "invalid"}), 400

    session_doc = get_or_create_session(contact)

    last_text = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or data.get("last_input")
    )

    if not last_text:
        return jsonify({"status": "no_text"})

    add_to_batch(session_doc, last_text)

    return jsonify({"status": "received"})


# ------------------------------------------------
# Health
# ------------------------------------------------
@app.route("/")
def home():
    return "Workflow Bot Running"


# ------------------------------------------------
# Run
# ------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Running on :{port}")
    app.run(host="0.0.0.0", port=port)
