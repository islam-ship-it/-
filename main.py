#!/usr/bin/env python3
import os
import json
import logging
import threading
import time
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("chatkit-proxy")

# ---------------- env ----------------
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")   # wf_...
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
PORT = int(os.getenv("PORT", 5000))
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", 2.0))
SESSION_TTL = int(os.getenv("SESSION_TTL", 600))  # seconds to keep chatkit session in cache

# sanity check
missing = [k for k in ("OPENAI_API_KEY", "WORKFLOW_ID", "MANYCHAT_API_KEY", "MANYCHAT_SECRET_KEY") if not globals().get(k)]
if missing:
    logger.critical(f"Missing env vars: {missing}")
    raise SystemExit(1)

app = Flask(__name__)

# ---------------- in-memory state ----------------
pending_messages = {}   # user_id -> {"texts": [], "platform": "Facebook"}
message_timers = {}     # user_id -> Timer
processing_locks = {}   # user_id -> Lock

# session cache: user_id -> {"session_id": "...", "created_at": unix_ts, "client_secret": optional}
session_cache = {}

# ---------------- helpers: ChatKit API ----------------
CHATKIT_BASE = "https://api.openai.com/v1"
CHATKIT_HEADERS_BASE = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "OpenAI-Beta": "chatkit_beta=v1",
    "Content-Type": "application/json"
}

def create_chatkit_session(workflow_id, user_id):
    """
    Creates a ChatKit session:
    POST /v1/chatkit/sessions
    Body: {"workflow": {"id": "wf_..."}, "user": "user_id"}
    Returns JSON or raises requests.HTTPError
    """
    url = f"{CHATKIT_BASE}/chatkit/sessions"
    payload = {
        "workflow": {"id": workflow_id},
        "user": user_id
    }
    r = requests.post(url, json=payload, headers=CHATKIT_HEADERS_BASE, timeout=60)
    if r.status_code >= 400:
        logger.error("ChatKit CREATE SESSION Error: %s", r)
        logger.error("Response body: %s", r.text)
        return {"__error": True, "status": r.status_code, "body": r.text}
    try:
        return r.json()
    except Exception:
        logger.exception("Invalid JSON from create_chatkit_session")
        return {"__error": True, "exception": True, "raw": r.text}

def send_chatkit_message(session_id, role, content):
    """
    Sends a message in an existing ChatKit session:
    POST /v1/chatkit/messages
    Body: {"session_id": "...", "role": "user"/"system", "content": "..."}
    Returns JSON or error dict.
    """
    url = f"{CHATKIT_BASE}/chatkit/messages"
    payload = {
        "session_id": session_id,
        "role": role,
        "content": content
    }
    r = requests.post(url, json=payload, headers=CHATKIT_HEADERS_BASE, timeout=60)
    if r.status_code >= 400:
        logger.error("ChatKit SEND MESSAGE Error: %s", r)
        logger.error("Response body: %s", r.text)
        return {"__error": True, "status": r.status_code, "body": r.text}
    try:
        return r.json()
    except Exception:
        logger.exception("Invalid JSON from send_chatkit_message")
        return {"__error": True, "exception": True, "raw": r.text}

def get_or_create_session_for_user(user_id):
    """
    Return session_id for user. Reuse cached if not expired, otherwise create new.
    Cache stores 'session_id' and 'created_at'.
    """
    now = int(time.time())
    entry = session_cache.get(user_id)
    if entry:
        age = now - int(entry.get("created_at", 0))
        if age < SESSION_TTL and entry.get("session_id"):
            return entry["session_id"]
        # expired -> drop
        session_cache.pop(user_id, None)

    # create new session
    resp = create_chatkit_session(WORKFLOW_ID, user_id)
    if resp.get("__error"):
        return None, resp
    # expected resp likely contains session_id and client_secret etc.
    # check likely keys: 'id' or 'session_id' or 'session'
    session_id = resp.get("id") or resp.get("session_id") or (resp.get("session") and resp["session"].get("id"))
    if not session_id:
        # fallback: maybe resp has client_secret but different structure
        logger.error("Create session missing session id. full response: %s", resp)
        return None, {"__error": True, "body": resp}
    session_cache[user_id] = {"session_id": session_id, "created_at": now, "raw": resp}
    return session_id, resp

# ---------------- helpers: extract assistant reply ----------------
def extract_assistant_from_message_response(resp):
    """
    After sending message, ChatKit may return object with 'messages' or 'outputs'.
    Try to find assistant message content.
    """
    if not isinstance(resp, dict):
        return None
    # direct messages array
    messages = resp.get("messages") or resp.get("result") or resp.get("outputs")
    if isinstance(messages, list):
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "assistant":
                # content can be string or dict
                content = m.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, dict):
                    # try common fields
                    return content.get("text") or content.get("content") or json.dumps(content)
    # some responses embed assistant reply in top-level 'assistant' or 'output'
    if "assistant" in resp:
        a = resp["assistant"]
        if isinstance(a, dict):
            return a.get("content") or a.get("text")
    # fallback to stringifying
    try:
        return json.dumps(resp)[:2000]
    except:
        return None

# ---------------- ManyChat send ----------------
def send_manychat_reply(subscriber_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform.lower() == "instagram" else "facebook"
    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text}] }},
        "channel": channel
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
        logger.info("Sent ManyChat reply to %s", subscriber_id)
        return True
    except Exception:
        logger.exception("ManyChat send failed")
        return False

# ---------------- processing (batch) ----------------
def schedule_processing(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages:
            return
        entry = pending_messages[user_id]
        text = "\n".join(entry["texts"]).strip()
        platform = entry["platform"]
        logger.info("Processing for %s: %s", user_id, text[:300])

        # 1) get or create session
        session_id, sess_resp = get_or_create_session_for_user(user_id)
        if not session_id:
            # failed to create session
            body = sess_resp.get("body") if isinstance(sess_resp, dict) else sess_resp
            logger.error("Failed to get session for %s: %s", user_id, body)
            send_manychat_reply(user_id, "⚠ حدث خطأ أثناء إنشاء الجلسة.", platform)
        else:
            # 2) send message to session
            send_resp = send_chatkit_message(session_id, "user", text)
            if send_resp.get("__error"):
                logger.error("Error sending message for %s: %s", user_id, send_resp.get("body"))
                send_manychat_reply(user_id, "⚠ حدث خطأ أثناء إرسال الرسالة إلى ChatKit.", platform)
            else:
                # 3) extract assistant reply
                assistant = extract_assistant_from_message_response(send_resp)
                if not assistant:
                    logger.warning("No assistant reply found, full send_resp: %s", send_resp)
                    assistant = "⚠ لم يتم الحصول على رد من المساعد."
                send_manychat_reply(user_id, assistant, platform)

        # cleanup
        pending_messages.pop(user_id, None)
        timer = message_timers.pop(user_id, None)
        if timer:
            try:
                timer.cancel()
            except:
                pass
        logger.info("Finished processing %s", user_id)

def add_message(user_id, text, platform):
    if user_id in message_timers:
        try:
            message_timers[user_id].cancel()
        except:
            pass
    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "platform": platform}
    pending_messages[user_id]["texts"].append(text)
    timer = threading.Timer(BATCH_WAIT_TIME, schedule_processing, args=[user_id])
    message_timers[user_id] = timer
    timer.start()
    logger.info("Queued message for %s (batch=%d)", user_id, len(pending_messages[user_id]["texts"]))

# ---------------- webhook ----------------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():
    auth = request.headers.get("Authorization")
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        logger.warning("Unauthorized webhook call")
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact")
    if not contact:
        return jsonify({"error": "invalid"}), 400

    user_id = str(contact.get("id"))
    platform = "Instagram" if "instagram" in str(contact.get("source", "")).lower() else "Facebook"

    last = (contact.get("last_text_input") or contact.get("last_input_text") or data.get("last_input") or "")
    if not str(last).strip():
        return jsonify({"status": "empty"})

    add_message(user_id, str(last), platform)
    return jsonify({"status": "received"})

@app.route("/")
def home():
    return "✅ ChatKit Workflow Proxy (sessions+messages) running"

# ---------------- run ----------------
if __name__ == "__main__":
    logger.info("Starting ChatKit proxy...")
    app.run(host="0.0.0.0", port=PORT)
