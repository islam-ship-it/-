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
log = logging.getLogger("manychat-responses-mongo")

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
SESSION_TTL = int(os.getenv("SESSION_TTL", 3600))  # seconds to reuse conversation
MAX_MEMORY_MESSAGES = int(os.getenv("MAX_MEMORY_MESSAGES", 20))  # how many past messages to keep

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
db = mongo.get_database()  # uses DB from connection string or default
sessions_col = db.get_collection("bot_sessions")       # store mapping user_id -> conversation_id, last_seen
messages_col = db.get_collection("bot_messages")       # store each message (user/assistant)
attachments_col = db.get_collection("bot_attachments") # optional attachment records
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
# Helpers: Mongo session management
# -------------------------
def now_ts():
    return int(time.time())

def get_session_doc(user_id: str) -> Optional[dict]:
    return sessions_col.find_one({"user_id": user_id})

def create_or_refresh_session(user_id: str) -> dict:
    """
    Ensure we have a conversation id for this user.
    Reuse if not expired; otherwise create a new conversation via OpenAI Conversations API.
    """
    entry = sessions_col.find_one({"user_id": user_id})
    if entry:
        age = now_ts() - int(entry.get("created_at", 0))
        if age < SESSION_TTL and entry.get("conversation_id"):
            return entry
        # expired -> we'll create new
    # Create new conversation via OpenAI Conversations API
    try:
        conv = client.conversations.create()
        conv_id = conv.get("id") if isinstance(conv, dict) else getattr(conv, "id", None)
        if not conv_id:
            raise ValueError("No conversation id returned")
    except Exception as e:
        log.exception("Failed to create conversation with OpenAI: %s", e)
        return {"__error": True, "error": str(e)}
    doc = {
        "user_id": user_id,
        "conversation_id": conv_id,
        "created_at": now_ts(),
        "updated_at": now_ts()
    }
    sessions_col.update_one({"user_id": user_id}, {"$set": doc}, upsert=True)
    return doc

# -------------------------
# Helpers: storing messages / attachments
# -------------------------
def store_message(user_id: str, role: str, content: str, raw=None):
    doc = {
        "user_id": user_id,
        "role": role,
        "content": content,
        "raw": raw or {},
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

# -------------------------
# Helpers: build input items for Responses API (include attachments when possible)
# -------------------------
def build_input_items(user_text: str, attachment_urls: List[Dict]) -> List[dict]:
    """
    Returns list of input items for responses.create.
    attachment_urls: list of dicts like {"url": "...", "type": "image"|"audio"}
    We'll put user text as input_text and append image items separately.
    """
    items = []
    if user_text:
        items.append({"role": "user", "content": user_text})
    # For attachments, Responses API can accept items via conversation or specialized fields.
    # We'll include them as content objects referencing the URL (many backends accept input_image)
    for a in attachment_urls:
        kind = a.get("type", "image")
        url = a.get("url")
        if not url:
            continue
        if kind == "image":
            # Represent as an additional input item (some SDKs accept structured items)
            items.append({"role": "user", "content": f"[image] {url}"})
        elif kind == "audio":
            items.append({"role": "user", "content": f"[audio] {url}"})
        else:
            items.append({"role": "user", "content": f"[attachment:{kind}] {url}"})
    return items

# -------------------------
# Helpers: call Responses API
# -------------------------
def call_responses_api(prompt_id: str, conversation_id: Optional[str], input_items: List[dict]) -> dict:
    """
    Use client.responses.create with prompt and conversation when available.
    Returns response dict or error dict.
    """
    kwargs = {"prompt": {"id": prompt_id}, "input": input_items}
    if conversation_id:
        kwargs["conversation"] = conversation_id
    try:
        resp = client.responses.create(**kwargs)
        # make sure resp is a dict
        return resp if isinstance(resp, dict) else resp.to_dict()
    except Exception as e:
        log.exception("OpenAI responses.create error: %s", e)
        return {"__error": True, "error": str(e)}

# -------------------------
# Helpers: extract assistant reply text from response object
# -------------------------
def extract_reply_text(resp: dict) -> str:
    if not isinstance(resp, dict):
        try:
            return str(resp)
        except:
            return ""
    # prefer 'output_text' or 'output' with text
    text = ""
    if "output_text" in resp and resp["output_text"]:
        return resp["output_text"]
    # else, try to parse output array
    out = resp.get("output") or resp.get("outputs") or resp.get("items") or resp.get("result") or []
    if isinstance(out, list):
        # find first assistant message
        for o in out:
            try:
                # many formats: o -> { "id":..., "content":[{"text": "..." }, ...], "role": "assistant"}
                if isinstance(o, dict):
                    # some objects include 'role'
                    role = o.get("role")
                    if role and role != "assistant":
                        continue
                    content = o.get("content") or o.get("content_text") or o.get("text")
                    if isinstance(content, list):
                        # find first item with text
                        for c in content:
                            if isinstance(c, dict) and c.get("text"):
                                return c.get("text")
                    if isinstance(content, str):
                        return content
                    # fallback to 'text' inside object
                    if o.get("text"):
                        return o.get("text")
            except Exception:
                continue
    # fallback to stringifying
    try:
        return json.dumps(resp)[:2000]
    except:
        return str(resp)

# -------------------------
# ManyChat send function
# -------------------------
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
# Main webhook endpoint
# -------------------------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():
    # auth
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

    # collect attachments if any (manychat uses last_attachments or custom fields)
    attachments = []
    # try common ManyChat fields
    mc_attachments = contact.get("last_attachments") or contact.get("attachments") or contact.get("media") or []
    if isinstance(mc_attachments, list):
        for a in mc_attachments:
            # each item may be url or dict
            if isinstance(a, dict):
                url = a.get("url") or a.get("image_url") or a.get("file_url")
                kind = a.get("type") or ("image" if url and url.lower().endswith((".jpg",".png",".jpeg")) else "file")
                if url:
                    attachments.append({"url": url, "type": kind})
            elif isinstance(a, str):
                url = a
                kind = "image" if url.lower().endswith((".jpg",".jpeg",".png",".webp")) else "file"
                attachments.append({"url": url, "type": kind})

    # fallback: sometimes ManyChat includes last_message with attachments
    last_message = contact.get("last_message") or {}
    if isinstance(last_message, dict):
        for key in ("image_url","file_url","audio_url","url"):
            url = last_message.get(key)
            if url:
                kind = "audio" if "audio" in key else ("image" if "image" in key else "file")
                attachments.append({"url": url, "type": kind})

    # store incoming user data
    store_message(user_id, "user", last_text or "[no text]", raw=contact)
    for a in attachments:
        store_attachment(user_id, a.get("url"), kind=a.get("type"))

    log.info("Queued incoming from %s (text len=%d, attachments=%d)", user_id, len(last_text), len(attachments))

    # create or reuse conversation
    sess = create_or_refresh_session(user_id)
    if sess is None or sess.get("__error"):
        err = sess.get("error") if isinstance(sess, dict) else "unknown"
        log.error("Failed creating session for %s: %s", user_id, err)
        send_manychat_reply(user_id, "⚠ حدث خطأ أثناء إنشاء الجلسة. حاول مرة أخرى لاحقًا.", platform)
        return jsonify({"status": "error", "reason": "session_failed"}), 500

    conversation_id = sess.get("conversation_id")

    # build input items (text + attachments)
    input_items = build_input_items(last_text, attachments)

    # to keep memory small, optionally load last N messages from DB and include as context (if you prefer)
    recent_msgs_cursor = messages_col.find({"user_id": user_id}).sort("created_at", -1).limit(MAX_MEMORY_MESSAGES)
    recent_msgs = list(reversed(list(recent_msgs_cursor)))
    # we won't inject them directly here because we're using conversation object — but you may choose to prepend a summary
    # Optionally: create a short memory summary and include as system message
    # Build a summary if too many messages (simple heuristic)
    if len(recent_msgs) > 8:
        # create simple summary of last few user messages
        summary_texts = [m["content"] for m in recent_msgs[-6:] if m.get("role") == "user"]
        summary = "ملخص سريع لآخر تفاعلات المستخدم: " + " | ".join(summary_texts)
        # include as initial system input if you want
        input_items.insert(0, {"role": "system", "content": summary})

    # call Responses API
    resp = call_responses_api(PROMPT_ID, conversation_id, input_items)

    if resp.get("__error"):
        log.error("OpenAI returned error for %s: %s", user_id, resp.get("error"))
        send_manychat_reply(user_id, "⚠ حدث خطأ أثناء معالجة الطلب. سيعمل فريقنا على إصلاحه.", platform)
        logs_col.insert_one({"type": "openai_error", "user_id": user_id, "error": resp.get("error"), "raw": resp, "ts": datetime.utcnow()})
        return jsonify({"status": "error", "reason": "openai_error"}), 500

    # extract assistant reply text
    assistant_text = extract_reply_text(resp) or "⚠ لم يتم توليد رد."

    # store assistant reply in DB
    store_message(user_id, "assistant", assistant_text, raw=resp)

    # if response contains attachments/tool outputs, try to detect them and store
    # e.g., resp.get("output") may contain items with URLs
    out_items = resp.get("output") or resp.get("outputs") or resp.get("items") or []
    if isinstance(out_items, list):
        for o in out_items:
            try:
                if isinstance(o, dict):
                    # try to find URLs in nested content
                    c = o.get("content") or []
                    if isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict):
                                text_val = part.get("text") or ""
                                # naive URL extraction
                                if isinstance(text_val, str) and ("http://" in text_val or "https://" in text_val):
                                    # find urls
                                    words = text_val.split()
                                    for w in words:
                                        if w.startswith("http://") or w.startswith("https://"):
                                            store_attachment(user_id, w, kind="output_link", meta={"source_part": part})
            except Exception:
                continue

    # send reply to ManyChat
    send_manychat_reply(user_id, assistant_text, platform)

    # update session timestamp
    sessions_col.update_one({"user_id": user_id}, {"$set": {"updated_at": now_ts()}})

    return jsonify({"status": "ok"}), 200

# -------------------------
# Health endpoint
# -------------------------
@app.route("/")
def home():
    return "🔥 ManyChat ↔ Responses API proxy – with Mongo Memory"

if __name__ == "__main__":
    log.info("Starting ManyChat-Responses proxy...")
    app.run(host="0.0.0.0", port=PORT)
