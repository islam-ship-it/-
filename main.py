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

# -------------------------
# Logging & env
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AGENT_ID = os.getenv("AGENT_ID")  # e.g. ag_xxx (required)
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", 2.0))

required = {
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "AGENT_ID": AGENT_ID,
    "MONGO_URI": MONGO_URI,
    "MANYCHAT_API_KEY": MANYCHAT_API_KEY,
    "MANYCHAT_SECRET_KEY": MANYCHAT_SECRET_KEY
}
missing = [k for k, v in required.items() if not v]
if missing:
    logger.critical(f"Missing env vars: {missing}. Fill .env and restart.")
    raise SystemExit(1)

# -------------------------
# Mongo
# -------------------------
client_db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client_db.get_database("multi_platform_bot")
sessions_col = db.get_collection("sessions")
messages_col = db.get_collection("messages")

try:
    client_db.admin.command("ping")
    logger.info("Connected to MongoDB")
except Exception:
    logger.exception("Mongo ping failed")
    raise

# -------------------------
# OpenAI client
# -------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# Flask app
# -------------------------
app = Flask(__name__)

# -------------------------
# Batching state
# -------------------------
pending_messages = {}   # user_id -> {"texts": [], "session": session_doc}
message_timers = {}     # user_id -> Timer
processing_locks = {}   # user_id -> Lock

# -------------------------
# Helpers
# -------------------------
def get_or_create_session(contact):
    user_id = str(contact.get("id"))
    if not user_id:
        return None

    doc = sessions_col.find_one({"_id": user_id})
    now = datetime.now(timezone.utc)

    source = str(contact.get("source", "")).lower()
    platform = "Instagram" if "instagram" in source else "Facebook"

    if doc:
        sessions_col.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact_date": now,
                "platform": platform,
                "profile.name": contact.get("name"),
                "profile.profile_pic": contact.get("profile_pic"),
                "status": "active"
            }}
        )
        return sessions_col.find_one({"_id": user_id})

    new_doc = {
        "_id": user_id,
        "platform": platform,
        "profile": {
            "name": contact.get("name"),
            "profile_pic": contact.get("profile_pic")
        },
        "agent_session_id": None,
        "created": now,
        "last_contact_date": now,
        "status": "active"
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
    except Exception:
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
            "content": {
                "messages": [
                    {"type": "text", "text": text.strip()}
                ]
            }
        },
        "channel": channel
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
        logger.info(f"Sent ManyChat reply to {subscriber_id}")
        return True
    except Exception:
        logger.exception("ManyChat send failed")
        return False


# -------------------------
# Agent session
# -------------------------
def ensure_agent_session_for_user(session_doc):
    user_id = session_doc["_id"]
    agent_session_id = session_doc.get("agent_session_id")
    if agent_session_id:
        return agent_session_id

    try:
        resp = client.agents.sessions.create(agent_id=AGENT_ID)
        agent_session_id = getattr(resp, "id", None)

        if not agent_session_id:
            logger.error("Agent session creation returned invalid response")
            return None

        sessions_col.update_one(
            {"_id": user_id},
            {"$set": {"agent_session_id": agent_session_id}}
        )
        logger.info(f"Created agent_session_id for {user_id}: {agent_session_id}")
        return agent_session_id

    except Exception:
        logger.exception("Failed creating agent session")
        return None


# -------------------------
# Call agent with memory
# -------------------------
def call_agent_with_session(agent_session_id, text, user_id):
    try:
        resp = client.agents.responses.create(
            agent_id=AGENT_ID,
            session_id=agent_session_id,
            input=text
        )

        if hasattr(resp, "output_text"):
            return resp.output_text

        try:
            return str(resp)
        except:
            return "خطأ في استخراج الرد."

    except Exception:
        logger.exception("Agent call failed")
        return "⚠️ حصل خطأ أثناء التواصل مع الوكيل."


# -------------------------
# Batching
# -------------------------
def schedule_processing(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())

    with lock:
        if user_id not in pending_messages:
            return

        data = pending_messages[user_id]
        session_doc = data["session"]
        texts = data["texts"]
        combined = "\n".join(texts).strip()

        save_message(user_id, "user", combined)

        agent_session_id = ensure_agent_session_for_user(session_doc)
        if not agent_session_id:
            reply = "⚠️ حصل خطأ في تهيئة المحادثة."
        else:
            reply = call_agent_with_session(agent_session_id, combined, user_id)

        save_message(user_id, "assistant", reply)
        send_manychat_reply(user_id, reply, session_doc.get("platform"))

        pending_messages.pop(user_id, None)
        t = message_timers.pop(user_id, None)
        if t:
            try:
                t.cancel()
            except:
                pass


def add_to_queue(session_doc, text):
    user_id = session_doc["_id"]

    if user_id in message_timers:
        try:
            message_timers[user_id].cancel()
        except:
            pass

    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session_doc}

    pending_messages[user_id]["texts"].append(text)

    timer = threading.Timer(BATCH_WAIT_TIME, schedule_processing, args=[user_id])
    message_timers[user_id] = timer
    timer.start()


# -------------------------
# ManyChat webhook
# -------------------------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():
    auth = request.headers.get("Authorization")
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact")

    if not contact:
        return jsonify({"error": "invalid"}), 400

    session_doc = get_or_create_session(contact)

    last_input = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or data.get("last_input")
    )

    if not last_input or str(last_input).strip() == "":
        return jsonify({"status": "no_input"})

    add_to_queue(session_doc, last_input)

    return jsonify({"status": "received"})


# -------------------------
# Health Check
# -------------------------
@app.route("/")
def home():
    return "Agents Bot Running"


# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
