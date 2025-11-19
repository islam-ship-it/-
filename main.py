#!/usr/bin/env python3
import os
import time
import json
import logging
import threading
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# --------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("chatkit-proxy")

# --------- env ----------
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")   # wf_....
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

PORT = int(os.getenv("PORT", 5000))
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", 2.0))

# sanity
missing = [k for k in ("OPENAI_API_KEY", "WORKFLOW_ID", "MANYCHAT_API_KEY", "MANYCHAT_SECRET_KEY") if not globals().get(k)]
if missing:
    logger.critical(f"Missing env vars: {missing}")
    raise SystemExit(1)

app = Flask(__name__)

# batching
pending_messages = {}
message_timers = {}
processing_locks = {}

# --------- ChatKit API CALL (الصح) ----------
def call_chatkit_workflow(workflow_id, user_message):
    url = "https://api.openai.com/v1/chatkits/sessions"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "workflow_id": workflow_id,
        "messages": [
            {
                "role": "user",
                "content": user_message
            }
        ]
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        r.raise_for_status()
        return r.json()

    except requests.exceptions.HTTPError as e:
        logger.error("ChatKit session error: %s — body: %s", e, getattr(e.response, "text", None))
        return {"__error": True, "status_code": r.status_code, "text": r.text}
    except Exception:
        logger.exception("ChatKit exception")
        return {"__error": True, "exception": True}

# --------- Extract reply ---------
def find_first_key(obj, keys):
    if obj is None:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v:
                return v
            res = find_first_key(v, keys)
            if res:
                return res
    if isinstance(obj, list):
        for item in obj:
            res = find_first_key(item, keys)
            if res:
                return res
    return None

def process_chatkit(workflow_id, text):
    resp = call_chatkit_workflow(workflow_id, text)

    if resp.get("__error"):
        return "⚠ حدث خطأ أثناء الاتصال بسير العمل."

    reply = find_first_key(resp, ["content", "text", "reply_text", "message"])

    if not reply:
        try:
            reply = json.dumps(resp)[:2000]
        except:
            reply = "⚠ لا يوجد رد من ChatKit."

    return reply

# --------- ManyChat send ----------
def send_manychat_reply(subscriber_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}

    channel = "instagram" if platform.lower() == "instagram" else "facebook"

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {
            "version": "v2",
            "content": {
                "messages": [{"type": "text", "text": text.strip()}]
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

# --------- batching logic ----------
def schedule_processing(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:

        if user_id not in pending_messages:
            return

        entry = pending_messages[user_id]
        text = "\n".join(entry["texts"])
        platform = entry["platform"]

        logger.info(f"Processing for {user_id}: {text[:200]}")

        reply_text = process_chatkit(WORKFLOW_ID, text)
        send_manychat_reply(user_id, reply_text, platform)

        pending_messages.pop(user_id, None)
        t = message_timers.pop(user_id, None)
        if t:
            t.cancel()

        logger.info(f"Finished processing {user_id}")

def add_message(user_id, text, platform):
    if user_id in message_timers:
        try: message_timers[user_id].cancel()
        except: pass

    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "platform": platform}

    pending_messages[user_id]["texts"].append(text)

    logger.info(f"Queued message for {user_id}; batch size {len(pending_messages[user_id]['texts'])}")

    timer = threading.Timer(BATCH_WAIT_TIME, schedule_processing, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

# --------- webhook ----------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():
    auth = request.headers.get("Authorization")
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact")

    if not contact:
        return jsonify({"error": "invalid"}), 400

    user_id = str(contact.get("id"))
    platform = "Instagram" if "instagram" in str(contact.get("source", "")).lower() else "Facebook"

    last_input = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or data.get("last_input")
        or ""
    )

    if not str(last_input).strip():
        return jsonify({"status": "empty"})

    add_message(user_id, last_input, platform)

    return jsonify({"status": "received"})

@app.route("/")
def home():
    return "✅ ChatKit Workflow Proxy Running"

if __name__ == "__main__":
    logger.info("Starting ChatKit proxy...")
    app.run(host="0.0.0.0", port=PORT)
