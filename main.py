# Full Python bot with OpenAI Agents API
# Includes: Persistent session_id, streaming responses, MongoDB memory, ManyChat integration

# NOTE: This is a template. Replace environment variables in .env

import os
import time
import json
import requests
import threading
import logging
from flask import Flask, request, jsonify
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv
from openai import OpenAI

# ------------------- INIT --------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
AGENT_ID = os.getenv("AGENT_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

pending_messages = {}
message_timers = {}
processing_locks = {}
BATCH_WAIT_TIME = 2.0

# ------------------- DB --------------------
client_db = MongoClient(MONGO_URI)
db = client_db["multi_platform_bot"]
sessions_collection = db["sessions"]

# ----------------- UTILITIES ----------------

def get_or_create_session(contact_data):
    user_id = str(contact_data.get("id"))
    if not user_id:
        return None

    now = datetime.now(timezone.utc)
    session = sessions_collection.find_one({"_id": user_id})

    source = str(contact_data.get("source", "")).lower()
    platform = "Instagram" if "instagram" in source else "Facebook"

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
        "agent_session_id": None
    }

    sessions_collection.insert_one(new_session)
    return new_session


def send_manychat_reply(subscriber_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    channel = "instagram" if platform == "Instagram" else "facebook"
    reply_text = text.strip()[:2000]

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {
            "version": "v2",
            "content": {
                "messages": [{"type": "text", "text": reply_text}]
            }
        },
        "channel": channel
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Send error: {e}")


# ------------------ OPENAI AGENT ------------------

def run_agent_workflow(text, session):
    """
    Streaming + Persistent agent session_id saved in Mongo.
    """
    try:
        # Use existing session_id or create new one
        agent_session_id = session.get("agent_session_id")

        if not agent_session_id:
            new_session = client.agents.sessions.create(agent_id=AGENT_ID)
            agent_session_id = new_session.id
            sessions_collection.update_one(
                {"_id": session["_id"]},
                {"$set": {"agent_session_id": agent_session_id}}
            )

        # ------ STREAMING ------
        final_text = ""

        with client.agents.responses.stream(
            agent_id=AGENT_ID,
            session_id=agent_session_id,
            input=text
        ) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    final_text += event.delta

        return final_text.strip()

    except Exception as e:
        logger.error(f"Agent error: {e}")
        return "⚠️ حدث خطأ أثناء التواصل مع وكيل الذكاء الاصطناعي."


# ----------------- BATCHING -----------------

def schedule_message_processing(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages:
            return

        data = pending_messages[user_id]
        session = data["session"]
        combined = "\n".join(data["texts"])

        reply = run_agent_workflow(combined, session)
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


# ------------------ WEBHOOK --------------------
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


@app.route("/")
def home():
    return "🚀 Bot Running with OpenAI Agent — Streaming + Memory"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
