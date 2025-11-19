#!/usr/bin/env python3
import os
import json
import logging
import time
from datetime import datetime
from typing import Optional, List

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from openai import OpenAI
from pymongo import MongoClient, ASCENDING

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("manychat-assistant-mongo")

# -------------------------
# Load env
# -------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")
PORT = int(os.getenv("PORT", 5000))

if not (OPENAI_API_KEY and ASSISTANT_ID and MANYCHAT_API_KEY and MANYCHAT_SECRET_KEY and MONGO_URI):
    log.critical("Missing required env vars.")
    raise SystemExit(1)

# -------------------------
# OpenAI Client (OLD API)
# -------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# MongoDB
# -------------------------
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo.get_database()
sessions_col = db.get_collection("sessions")
messages_col = db.get_collection("messages")

sessions_col.create_index([("user_id", ASCENDING)], unique=True)
messages_col.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])

# -------------------------
# Flask App
# -------------------------
app = Flask(__name__)

# -------------------------
# Helpers
# -------------------------
def get_or_create_thread(user_id):
    session = sessions_col.find_one({"user_id": user_id})
    if session and session.get("thread_id"):
        return session["thread_id"]

    thread = client.beta.threads.create()
    thread_id = thread.id

    sessions_col.update_one(
        {"user_id": user_id},
        {"$set": {"thread_id": thread_id}},
        upsert=True
    )

    return thread_id


def send_to_assistant(thread_id, text):
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=text
    )

    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID
    )

    while True:
        run_status = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run_status.status in ["completed", "failed"]:
            break
        time.sleep(0.5)

    if run_status.status == "failed":
        return "❌ حدث خطأ."

    msgs = client.beta.threads.messages.list(thread_id=thread_id)
    for m in reversed(msgs.data):
        if m.role == "assistant":
            return m.content[0].text.value

    return "❌ لم يتم العثور على رد."


def send_manychat_reply(sub_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "subscriber_id": str(sub_id),
        "channel": "instagram" if platform == "instagram" else "facebook",
        "data": {
            "version": "v2",
            "content": {
                "messages": [{"type": "text", "text": text}]
            }
        }
    }

    requests.post(url, headers=headers, json=payload)


@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():
    if request.headers.get("Authorization") != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact") or {}

    user_id = str(contact.get("id") or contact.get("subscriber_id"))
    text = (contact.get("last_text_input") or "").strip()
    platform = "instagram" if "instagram" in str(contact.get("source", "")).lower() else "facebook"

    thread_id = get_or_create_thread(user_id)
    reply = send_to_assistant(thread_id, text)

    send_manychat_reply(user_id, reply, platform)
    return jsonify({"status": "ok"})


@app.route("/")
def home():
    return "🔥 ManyChat ↔ OpenAI Assistants API (OLD) — Running!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
