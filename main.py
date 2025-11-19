#!/usr/bin/env python3
import os
import json
import logging
import time
import uuid
from datetime import datetime
from typing import List, Optional, Dict

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError

# ------------------------------------------
# Logging
# ------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("manychat-openai-bot")

# ------------------------------------------
# Load ENV
# ------------------------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PROMPT_ID = os.getenv("PROMPT_ID")               # pmpt_xxx (Studio Prompt or Workflow)
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "manychatdb")

PORT = int(os.getenv("PORT", "10000"))
MAX_SUMMARY_MESSAGES = 20

if not (OPENAI_API_KEY and PROMPT_ID and MANYCHAT_API_KEY and MANYCHAT_SECRET_KEY and MONGO_URI):
    log.critical("Missing environment variables")
    raise SystemExit(1)

# ------------------------------------------
# MongoDB
# ------------------------------------------
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo[MONGO_DB]

sessions_col = db["bot_sessions"]
messages_col = db["bot_messages"]
attachments_col = db["bot_attachments"]

sessions_col.create_index([("user_id", ASCENDING)], unique=True)
messages_col.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
attachments_col.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])

# ------------------------------------------
# Flask
# ------------------------------------------
app = Flask(__name__)

# ------------------------------------------
# Helpers
# ------------------------------------------
def now_ts():
    return int(time.time())


def create_conversation_for_user(user_id: str) -> str:
    """Generate local conversation_id (no OpenAI conversation API)."""
    s = sessions_col.find_one({"user_id": user_id})
    if s:
        return s["conversation_id"]

    conv_id = str(uuid.uuid4())

    try:
        sessions_col.insert_one({
            "user_id": user_id,
            "conversation_id": conv_id,
            "created_at": now_ts(),
            "updated_at": now_ts()
        })
    except DuplicateKeyError:
        # if race condition
        s = sessions_col.find_one({"user_id": user_id})
        return s["conversation_id"]

    return conv_id


def store_message(user_id, role, content, raw=None):
    messages_col.insert_one({
        "user_id": user_id,
        "role": role,
        "content": content,
        "raw": raw or {},
        "created_at": datetime.utcnow()
    })


def store_attachment(user_id, url, kind="file", meta=None):
    attachments_col.insert_one({
        "user_id": user_id,
        "url": url,
        "kind": kind,
        "meta": meta or {},
        "created_at": datetime.utcnow()
    })


def get_recent_messages(user_id, limit=40):
    cursor = messages_col.find({"user_id": user_id}).sort("created_at", -1).limit(limit)
    return list(reversed(list(cursor)))


def build_input_items(user_text, attachments):
    items = []
    if user_text:
        items.append({"role": "user", "content": user_text})

    for a in attachments:
        url = a["url"]
        kind = a.get("type", "file")
        items.append({"role": "user", "content": f"[{kind}] {url}"})

    return items


# ------------------------------------------
# 🔥 Responses API — Official REST Call
# ------------------------------------------
def call_responses_api(prompt_id: str, conversation_id: str, input_items: List[dict]) -> dict:

    url = "https://api.openai.com/v1/responses"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": prompt_id,                 # this is your STUDIO PROMPT/WORKFLOW
        "input": input_items,               # user messages
        "conversation": conversation_id     # local conversation id
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=40)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        log.exception("Responses API Error: %s", e)
        return {"__error": True, "error": str(e)}


# ------------------------------------------
# Extract final reply text from responses API
# ------------------------------------------
def extract_reply_text(resp: dict) -> str:

    # direct output_text
    if resp.get("output_text"):
        return resp["output_text"]

    # check output items
    output = resp.get("output") or resp.get("outputs") or resp.get("items") or []
    if isinstance(output, list):
        for item in output:
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if part.get("text"):
                        return part["text"]

    return "⚠ لم يتم توليد رد."


# ------------------------------------------
# ManyChat sender
# ------------------------------------------
def send_manychat_reply(sub_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}

    channel = "instagram" if platform.lower() == "instagram" else "facebook"

    payload = {
        "subscriber_id": str(sub_id),
        "channel": channel,
        "data": {
            "version": "v2",
            "content": {"messages": [{"type": "text", "text": text}]}
        }
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
    except Exception:
        log.exception("Failed sending ManyChat reply to %s", sub_id)


# ------------------------------------------
# Webhook
# ------------------------------------------
@app.route("/manychat_webhook", methods=["POST"])
def webhook():

    if request.headers.get("Authorization") != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact") or {}

    user_id = str(contact.get("id") or contact.get("subscriber_id") or "unknown")
    text = (contact.get("last_text_input") or "").strip()
    platform = "Instagram" if "instagram" in str(contact.get("source", "")).lower() else "Facebook"

    # attachments
    attachments = []
    for a in contact.get("last_attachments", []):
        url = a.get("url") or a.get("image_url") or a.get("file_url")
        if url:
            attachments.append({"url": url, "type": a.get("type", "file")})

    # Save incoming
    store_message(user_id, "user", text or "[no text]", raw=contact)
    for a in attachments:
        store_attachment(user_id, a["url"], a["type"])

    # Local conversation ID
    conv_id = create_conversation_for_user(user_id)

    # Build input
    items = build_input_items(text, attachments)

    # Optional memory summary
    msgs = get_recent_messages(user_id)
    if len(msgs) > MAX_SUMMARY_MESSAGES:
        summary = "ملخص سابق: " + " | ".join(m["content"] for m in msgs[-10:])
        items.insert(0, {"role": "system", "content": summary})

    # Call Responses API
    resp = call_responses_api(PROMPT_ID, conv_id, items)

    if resp.get("__error"):
        send_manychat_reply(user_id, "⚠ حدث خطأ أثناء المعالجة.", platform)
        return jsonify({"error": "openai_failed"}), 500

    # Extract reply
    reply = extract_reply_text(resp)

    # Save assistant reply
    store_message(user_id, "assistant", reply, raw=resp)

    # Send to ManyChat
    send_manychat_reply(user_id, reply, platform)

    return jsonify({"status": "ok"}), 200


@app.route("/")
def home():
    return "🔥 ManyChat ↔ OpenAI Responses API — Bot is running!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
