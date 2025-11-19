#!/usr/bin/env python3
import os
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Optional, List, Dict

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from openai import OpenAI
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("manychat-bot")

# Load env
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PROMPT_ID = os.getenv("PROMPT_ID")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "manychatdb")
PORT = int(os.getenv("PORT", "10000"))

# Check required env
if not (OPENAI_API_KEY and PROMPT_ID and MANYCHAT_API_KEY and MANYCHAT_SECRET_KEY and MONGO_URI):
    log.critical("Missing environment variables")
    raise SystemExit(1)

# OpenAI Client
client = OpenAI(api_key=OPENAI_API_KEY)

# MongoDB
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo[MONGO_DB]

sessions_col = db.get_collection("bot_sessions")
messages_col = db.get_collection("bot_messages")
attachments_col = db.get_collection("bot_attachments")


# Helpers
def now_ts():
    return int(time.time())


def create_conversation_for_user(user_id: str):
    """Use local UUID conversation ID instead of Conversations API."""
    sess = sessions_col.find_one({"user_id": user_id})
    if sess:
        return sess["conversation_id"]

    conv_id = str(uuid.uuid4())

    try:
        sessions_col.insert_one({
            "user_id": user_id,
            "conversation_id": conv_id,
            "created_at": now_ts(),
            "updated_at": now_ts()
        })
    except DuplicateKeyError:
        sess = sessions_col.find_one({"user_id": user_id})
        return sess["conversation_id"]

    return conv_id


def store_message(user_id, role, content, raw=None, response_id=None):
    messages_col.insert_one({
        "user_id": user_id,
        "role": role,
        "content": content,
        "raw": raw or {},
        "response_id": response_id,
        "created_at": datetime.utcnow()
    })


def store_attachment(user_id, url, kind="image", meta=None):
    attachments_col.insert_one({
        "user_id": user_id,
        "url": url,
        "kind": kind,
        "meta": meta or {},
        "created_at": datetime.utcnow()
    })


def get_recent_user_items(user_id, limit=50):
    cursor = messages_col.find({"user_id": user_id}).sort("created_at", -1).limit(limit)
    return list(reversed(list(cursor)))


def build_input_items(text, attachments):
    items = []
    if text:
        items.append({"role": "user", "content": text})

    for a in attachments:
        url = a.get("url")
        t = a.get("type", "file")
        if url:
            items.append({"role": "user", "content": f"[{t}] {url}"})

    return items


# 🔥 Chat Completions API (بدل responses)
def call_openai_chat(prompt_id: str, input_items: List[dict]) -> dict:
    try:
        resp = client.chat.completions.create(
            model=prompt_id,
            messages=input_items
        )
        return resp.to_dict()
    except Exception as e:
        log.exception("OpenAI error: %s", e)
        return {"__error": True, "error": str(e)}


def extract_reply_text(resp):
    try:
        return resp["choices"][0]["message"]["content"]
    except:
        return "⚠ لم يتم توليد رد."


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
        log.exception("Failed to send reply to %s", sub_id)


# Flask App
app = Flask(__name__)


@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():

    if request.headers.get("Authorization") != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact") or {}

    user_id = str(contact.get("id") or contact.get("subscriber_id") or "unknown")
    last_text = (contact.get("last_text_input") or "").strip()
    platform = "Instagram" if "instagram" in str(contact.get("source", "")).lower() else "Facebook"

    attachments = []
    for a in contact.get("last_attachments", []):
        url = a.get("url") or a.get("image_url") or a.get("file_url")
        if url:
            attachments.append({"url": url, "type": a.get("type", "file")})

    # save incoming
    store_message(user_id, "user", last_text or "[no text]", raw=contact)
    for a in attachments:
        store_attachment(user_id, a["url"], a["type"])

    conv_id = create_conversation_for_user(user_id)

    input_items = build_input_items(last_text, attachments)

    # memory summary
    all_msgs = get_recent_user_items(user_id, limit=50)
    if len(all_msgs) > 15:
        summary = "ملخص سابق: " + " | ".join(m["content"] for m in all_msgs[-10:])
        input_items.insert(0, {"role": "system", "content": summary})

    # send to OpenAI
    resp = call_openai_chat(PROMPT_ID, input_items)

    if resp.get("__error"):
        send_manychat_reply(user_id, "⚠ حدث خطأ أثناء معالجة طلبك.", platform)
        return jsonify({"error": "openai_error"}), 500

    reply = extract_reply_text(resp)

    store_message(user_id, "assistant", reply, raw=resp)

    send_manychat_reply(user_id, reply, platform)

    sessions_col.update_one({"user_id": user_id}, {"$set": {"updated_at": now_ts()}})

    return jsonify({"status": "ok"}), 200


@app.route("/")
def home():
    return "🔥 ManyChat ↔ OpenAI Chat API – Bot is running!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
