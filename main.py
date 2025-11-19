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

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("manychat-responses-mongo-persistent")

# -------------------------
# Load env
# -------------------------
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PROMPT_ID = os.getenv("PROMPT_ID")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "manychatdb")
PORT = int(os.getenv("PORT", "10000"))
MAX_MEMORY_MESSAGES = int(os.getenv("MAX_MEMORY_MESSAGES", 100))

# Check env
if not (OPENAI_API_KEY and PROMPT_ID and MANYCHAT_API_KEY and MANYCHAT_SECRET_KEY and MONGO_URI):
    log.critical("Missing required environment variables")
    raise SystemExit(1)

# -------------------------
# OpenAI client
# -------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# MongoDB Setup
# -------------------------
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo[MONGO_DB]

sessions_col = db.get_collection("bot_sessions")
messages_col = db.get_collection("bot_messages")
attachments_col = db.get_collection("bot_attachments")
logs_col = db.get_collection("bot_logs")

# indexes
sessions_col.create_index([("user_id", ASCENDING)], unique=True)
messages_col.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
attachments_col.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])

# -------------------------
# Flask App
# -------------------------
app = Flask(__name__)

# -------------------------
# Helpers
# -------------------------
def now_ts():
    return int(time.time())

def create_conversation_for_user(user_id: str) -> Optional[str]:
    """
    Instead of using OpenAI Conversations API (removed from SDK),
    we generate a local conversation_id and store it in Mongo.
    """
    sess = sessions_col.find_one({"user_id": user_id})
    if sess and sess.get("conversation_id"):
        return sess["conversation_id"]

    # generate local conversation id
    conv_id = str(uuid.uuid4())

    doc = {
        "user_id": user_id,
        "conversation_id": conv_id,
        "created_at": now_ts(),
        "updated_at": now_ts()
    }

    try:
        sessions_col.insert_one(doc)
    except DuplicateKeyError:
        sess = sessions_col.find_one({"user_id": user_id})
        return sess.get("conversation_id")

    return conv_id


def store_message(user_id: str, role: str, content: str, raw=None, response_id=None):
    messages_col.insert_one({
        "user_id": user_id,
        "role": role,
        "content": content,
        "raw": raw or {},
        "response_id": response_id,
        "created_at": datetime.utcnow()
    })


def store_attachment(user_id: str, url: str, kind="image", meta=None):
    attachments_col.insert_one({
        "user_id": user_id,
        "url": url,
        "kind": kind,
        "meta": meta or {},
        "created_at": datetime.utcnow()
    })


def get_recent_user_items(user_id: str, limit=50) -> List[dict]:
    cursor = messages_col.find({"user_id": user_id}).sort("created_at", -1).limit(limit)
    return list(reversed(list(cursor)))


def build_input_items(text, attachments):
    items = []
    if text:
        items.append({"role": "user", "content": text})

    for a in attachments:
        url = a.get("url")
        t = a.get("type", "image")
        if not url:
            continue
        items.append({"role": "user", "content": f"[{t}] {url}"})

    return items


def call_responses_api(prompt_id: str, conv_id: Optional[str], input_items: List[dict]) -> dict:
    payload = {
        "prompt": {"id": prompt_id},
        "input": input_items,
        "conversation": conv_id
    }

    try:
        resp = client.responses.create(**payload)
        return resp.to_dict()
    except Exception as e:
        log.exception("OpenAI responses.create error: %s", e)
        return {"__error": True, "error": str(e)}


def extract_reply_text(resp):
    if not resp:
        return "⚠ خطأ في استخراج الرد."

    if resp.get("output_text"):
        return resp["output_text"]

    for item in resp.get("output", []):
        for part in item.get("content", []):
            if part.get("text"):
                return part["text"]

    return "⚠ لم يتم توليد رد."


def send_manychat_reply(sub_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform.lower() == "instagram" else "facebook"

    data = {
        "subscriber_id": str(sub_id),
        "channel": channel,
        "data": {
            "version": "v2",
            "content": {"messages": [{"type": "text", "text": text}]}
        }
    }

    try:
        r = requests.post(url, headers=headers, json=data, timeout=30)
        r.raise_for_status()
    except Exception:
        log.exception("ManyChat send failed for %s", sub_id)

# -------------------------
# Webhook Endpoint
# -------------------------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact") or {}

    user_id = str(contact.get("id") or contact.get("subscriber_id") or "unknown")
    last_text = (contact.get("last_text_input") or "").strip()
    platform = "Instagram" if "instagram" in str(contact.get("source", "")).lower() else "Facebook"

    # attachments
    attachments = []
    for a in contact.get("last_attachments", []):
        url = a.get("url") or a.get("image_url")
        if url:
            attachments.append({"url": url, "type": a.get("type", "image")})

    store_message(user_id, "user", last_text or "[no text]", raw=contact)
    for a in attachments:
        store_attachment(user_id, a["url"], kind=a["type"])

    conv_id = create_conversation_for_user(user_id)
    if not conv_id:
        send_manychat_reply(user_id, "⚠ حدث خطأ بإنشاء الجلسة.", platform)
        return jsonify({"error": "conv_failed"}), 500

    input_items = build_input_items(last_text, attachments)

    # optional summary
    msgs = get_recent_user_items(user_id, MAX_MEMORY_MESSAGES)
    if len(msgs) > 20:
        summary = "ملخص سريع: " + " | ".join(m["content"] for m in msgs[-10:] if m["role"] == "user")
        input_items.insert(0, {"role": "system", "content": summary})

    resp = call_responses_api(PROMPT_ID, conv_id, input_items)
    if resp.get("__error"):
        send_manychat_reply(user_id, "⚠ حدث خطأ أثناء المعالجة.", platform)
        return jsonify({"error": "openai_error"}), 500

    reply = extract_reply_text(resp)
    store_message(user_id, "assistant", reply, raw=resp, response_id=resp.get("id"))
    send_manychat_reply(user_id, reply, platform)
    sessions_col.update_one({"user_id": user_id}, {"$set": {"updated_at": now_ts()}})

    return jsonify({"status": "ok"}), 200


@app.route("/")
def home():
    return "🔥 ManyChat ↔ OpenAI Responses Proxy – Mongo Persistent Memory"


if __name__ == "__main__":
    log.info("Starting service...")
    app.run(host="0.0.0.0", port=PORT)
