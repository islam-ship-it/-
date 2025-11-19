#!/usr/bin/env python3
import os
import json
import logging
import time
from datetime import datetime
from typing import Optional, List, Dict

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from openai import OpenAI
from pymongo import MongoClient, ASCENDING

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
PORT = int(os.getenv("PORT", 5000))
MAX_MEMORY_MESSAGES = int(os.getenv("MAX_MEMORY_MESSAGES", 100))

if not (OPENAI_API_KEY and PROMPT_ID and MANYCHAT_API_KEY and MANYCHAT_SECRET_KEY and MONGO_URI):
    log.critical("Missing required env vars. Please set OPENAI_API_KEY, PROMPT_ID, MANYCHAT_API_KEY, MANYCHAT_SECRET_KEY, MONGO_URI")
    raise SystemExit(1)

# -------------------------
# OpenAI client
# -------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# MongoDB Setup
# -------------------------
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo.get_database()
sessions_col = db.get_collection("bot_sessions")       # { user_id, conversation_id, created_at, updated_at }
messages_col = db.get_collection("bot_messages")       # { user_id, role, content, raw, created_at, response_id(optional) }
attachments_col = db.get_collection("bot_attachments") # { user_id, url, kind, meta, created_at }
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
    Create a conversation via OpenAI and store it persistently for the user.
    If already exists, return existing conversation_id.
    """
    sess = sessions_col.find_one({"user_id": user_id})
    if sess and sess.get("conversation_id"):
        return sess["conversation_id"]
    try:
        conv = client.conversations.create()
        conv_id = conv.get("id") if isinstance(conv, dict) else getattr(conv, "id", None)
        if not conv_id:
            raise ValueError("No conversation id returned")
    except Exception as e:
        log.exception("Failed to create conversation for %s: %s", user_id, e)
        return None
    doc = {
        "user_id": user_id,
        "conversation_id": conv_id,
        "created_at": now_ts(),
        "updated_at": now_ts()
    }
    sessions_col.update_one({"user_id": user_id}, {"$set": doc}, upsert=True)
    return conv_id

def store_message(user_id: str, role: str, content: str, raw=None, response_id: Optional[str]=None):
    doc = {
        "user_id": user_id,
        "role": role,
        "content": content,
        "raw": raw or {},
        "response_id": response_id,
        "created_at": datetime.utcnow()
    }
    res = messages_col.insert_one(doc)
    return str(res.inserted_id)

def store_attachment(user_id: str, url: str, kind: str = "image", meta: dict = None):
    doc = {
        "user_id": user_id,
        "url": url,
        "kind": kind,
        "meta": meta or {},
        "created_at": datetime.utcnow()
    }
    res = attachments_col.insert_one(doc)
    return str(res.inserted_id)

def get_recent_user_items(user_id: str, limit: int = 50) -> List[dict]:
    cursor = messages_col.find({"user_id": user_id}).sort("created_at", -1).limit(limit)
    return list(reversed(list(cursor)))

def build_input_items(user_text: str, attachment_urls: List[Dict]) -> List[dict]:
    items = []
    if user_text:
        items.append({"role": "user", "content": user_text})
    for a in attachment_urls:
        kind = a.get("type", "image")
        url = a.get("url")
        if not url:
            continue
        if kind == "image":
            items.append({"role": "user", "content": f"[image] {url}"})
        elif kind == "audio":
            items.append({"role": "user", "content": f"[audio] {url}"})
        else:
            items.append({"role": "user", "content": f"[attachment:{kind}] {url}"})
    return items

def call_responses_api(prompt_id: str, conversation_id: Optional[str], input_items: List[dict]) -> dict:
    kwargs = {"prompt": {"id": prompt_id}, "input": input_items}
    if conversation_id:
        kwargs["conversation"] = conversation_id
    try:
        resp = client.responses.create(**kwargs)
        # ensure dict
        return resp if isinstance(resp, dict) else resp.to_dict()
    except Exception as e:
        log.exception("OpenAI responses.create error: %s", e)
        return {"__error": True, "error": str(e)}

def extract_reply_text(resp: dict) -> str:
    if not isinstance(resp, dict):
        return str(resp)
    if "output_text" in resp and resp["output_text"]:
        return resp["output_text"]
    out = resp.get("output") or resp.get("outputs") or resp.get("items") or []
    if isinstance(out, list):
        for o in out:
            try:
                if isinstance(o, dict):
                    role = o.get("role")
                    content = o.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("text"):
                                return part.get("text")
                    if isinstance(content, str):
                        return content
                    if o.get("text"):
                        return o.get("text")
            except Exception:
                continue
    try:
        return json.dumps(resp)[:2000]
    except:
        return str(resp)

def send_manychat_reply(sub_id: str, text: str, platform: str):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform.lower() == "instagram" else "facebook"
    payload = {
        "subscriber_id": str(sub_id),
        "channel": channel,
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text}]}}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        log.info("Sent ManyChat reply to %s", sub_id)
    except Exception:
        log.exception("ManyChat send failed for %s", sub_id)

# -------------------------
# Webhook
# -------------------------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact") or {}
    if not contact:
        return jsonify({"error": "invalid_payload"}), 400

    user_id = str(contact.get("id") or contact.get("subscriber_id") or data.get("subscriber_id") or "unknown")
    platform = "Instagram" if "instagram" in str(contact.get("source", "")).lower() else "Facebook"
    last_text = (contact.get("last_text_input") or contact.get("last_input_text") or data.get("last_input") or "").strip()

    attachments = []
    mc_attachments = contact.get("last_attachments") or contact.get("attachments") or contact.get("media") or []
    if isinstance(mc_attachments, list):
        for a in mc_attachments:
            if isinstance(a, dict):
                url = a.get("url") or a.get("image_url") or a.get("file_url")
                kind = a.get("type") or ("image" if url and url.lower().endswith((".jpg",".png",".jpeg")) else "file")
                if url:
                    attachments.append({"url": url, "type": kind})
            elif isinstance(a, str):
                url = a
                kind = "image" if url.lower().endswith((".jpg",".jpeg",".png",".webp")) else "file"
                attachments.append({"url": url, "type": kind})

    last_message = contact.get("last_message") or {}
    if isinstance(last_message, dict):
        for key in ("image_url","file_url","audio_url","url"):
            url = last_message.get(key)
            if url:
                kind = "audio" if "audio" in key else ("image" if "image" in key else "file")
                attachments.append({"url": url, "type": kind})

    # persist incoming
    store_message(user_id, "user", last_text or "[no text]", raw=contact)
    for a in attachments:
        store_attachment(user_id, a.get("url"), kind=a.get("type"))

    log.info("Incoming from %s (text len=%d, attachments=%d)", user_id, len(last_text), len(attachments))

    # ensure conversation exists (persistent)
    conv_id = create_conversation_for_user(user_id)
    if not conv_id:
        send_manychat_reply(user_id, "⚠ حدث خطأ بإنشاء الجلسة، حاول مره أخرى لاحقاً.", platform)
        logs_col.insert_one({"type":"session_error", "user_id": user_id, "payload": contact, "ts": datetime.utcnow()})
        return jsonify({"status": "error", "reason": "conv_create_failed"}), 500

    # build items
    input_items = build_input_items(last_text, attachments)

    # optional: include recent local context summary if many messages
    recent_msgs = get_recent_user_items(user_id, limit=MAX_MEMORY_MESSAGES)
    if len(recent_msgs) > 20:
        # build a compact summary string (very simple)
        last_texts = [m["content"] for m in recent_msgs if m.get("role") == "user"][-10:]
        summary = "ملخص سريع للمستخدم: " + " | ".join(last_texts)
        input_items.insert(0, {"role": "system", "content": summary})

    # call OpenAI Responses
    resp = call_responses_api(PROMPT_ID, conv_id, input_items)
    if resp.get("__error"):
        log.error("OpenAI error for %s: %s", user_id, resp.get("error"))
        logs_col.insert_one({"type":"openai_error", "user_id": user_id, "error": resp.get("error"), "raw": resp, "ts": datetime.utcnow()})
        send_manychat_reply(user_id, "⚠ حدث خطأ أثناء معالجة طلبك. حاول مرة أخرى لاحقاً.", platform)
        return jsonify({"status": "error", "reason": "openai_failed"}), 500

    assistant_text = extract_reply_text(resp) or "⚠ لم يتم توليد رد."
    # persist assistant reply
    store_message(user_id, "assistant", assistant_text, raw=resp, response_id=resp.get("id"))

    # try to detect and store any urls in outputs
    out_items = resp.get("output") or resp.get("outputs") or resp.get("items") or []
    if isinstance(out_items, list):
        for o in out_items:
            try:
                if isinstance(o, dict):
                    c = o.get("content") or []
                    if isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict):
                                text_val = part.get("text") or ""
                                if isinstance(text_val, str) and ("http://" in text_val or "https://" in text_val):
                                    words = text_val.split()
                                    for w in words:
                                        if w.startswith("http://") or w.startswith("https://"):
                                            store_attachment(user_id, w, kind="output_link", meta={"source_part": part})
            except Exception:
                continue

    # send to ManyChat
    send_manychat_reply(user_id, assistant_text, platform)
    # update session timestamp
    sessions_col.update_one({"user_id": user_id}, {"$set": {"updated_at": now_ts()}}, upsert=False)

    return jsonify({"status": "ok"}), 200

# -------------------------
# Health
# -------------------------
@app.route("/")
def home():
    return "🔥 ManyChat ↔ Responses API proxy – persistent memory (Mongo)"

if __name__ == "__main__":
    log.info("Starting ManyChat-Responses proxy (persistent memory)...")
    app.run(host="0.0.0.0", port=PORT)
