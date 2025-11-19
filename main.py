#!/usr/bin/env python3
import os
import json
import logging
import threading
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ------------------------------------------------------
# LOGGING
# ------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("chatkit-proxy")

# ------------------------------------------------------
# ENV
# ------------------------------------------------------
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")   # wf_....
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
PORT = int(os.getenv("PORT", 5000))
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", 2.0))

missing = [
    k for k in ("OPENAI_API_KEY", "WORKFLOW_ID", "MANYCHAT_API_KEY", "MANYCHAT_SECRET_KEY")
    if not globals().get(k)
]
if missing:
    logger.critical(f"Missing env vars: {missing}")
    raise SystemExit(1)

app = Flask(__name__)

pending_messages = {}
message_timers = {}
processing_locks = {}

# ------------------------------------------------------
# CHATKIT SESSION API (THE CORRECT ONE)
# ------------------------------------------------------
def call_chatkit(workflow_id, user_id, message_text):
    url = "https://api.openai.com/v1/chatkit/sessions"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "chatkit_beta=v1",
        "Content-Type": "application/json"
    }

    payload = {
        "workflow": {
            "id": workflow_id        # ← IMPORTANT: must be inside workflow{}
        },
        "user": user_id,
        "messages": [
            {
                "role": "user",
                "content": message_text
            }
        ]
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=60)

        if r.status_code >= 400:
            logger.error("❌ ChatKit Error: %s", r)
            logger.error("📩 ChatKit Response Body: %s", r.text)
            return {"__error": True, "body": r.text, "status": r.status_code}

        return r.json()

    except Exception as e:
        logger.exception("❌ ChatKit Exception")
        return {"__error": True, "exception": True}


# ------------------------------------------------------
# EXTRACT REPLY
# ------------------------------------------------------
def extract_reply(resp):
    try:
        msgs = resp.get("messages", [])
        for m in msgs:
            if m.get("role") == "assistant":
                return m.get("content", "لا يوجد رد.")
    except:
        pass
    return "⚠ حدث خطأ أثناء قراءة رد ChatKit."


# ------------------------------------------------------
# SEND MANYCHAT
# ------------------------------------------------------
def send_manychat_reply(subscriber_id, reply, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    channel = "instagram" if platform.lower() == "instagram" else "facebook"

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {
            "version": "v2",
            "content": {
                "messages": [{"type": "text", "text": reply}]
            }
        },
        "channel": channel
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        r.raise_for_status()
        logger.info(f"📨 Sent reply to {subscriber_id}")
        return True
    except Exception:
        logger.exception("❌ ManyChat Send Failed")
        return False


# ------------------------------------------------------
# PROCESSING (BATCH)
# ------------------------------------------------------
def schedule_processing(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:

        if user_id not in pending_messages:
            return

        entry = pending_messages[user_id]
        text = "\n".join(entry["texts"]).strip()
        platform = entry["platform"]

        logger.info(f"⚙️ Processing for {user_id}: {text}")

        # Call ChatKit
        resp = call_chatkit(WORKFLOW_ID, user_id, text)

        if resp.get("__error"):
            reply = "⚠ حدث خطأ أثناء الاتصال بسير العمل."
        else:
            reply = extract_reply(resp)

        send_manychat_reply(user_id, reply, platform)

        # Cleanup
        pending_messages.pop(user_id, None)
        t = message_timers.pop(user_id, None)
        if t:
            t.cancel()

        logger.info(f"✔️ Done processing {user_id}")


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

    logger.info(f"⏳ Queued message for {user_id} (batch={len(pending_messages[user_id]['texts'])})")


# ------------------------------------------------------
# MANYCHAT WEBHOOK
# ------------------------------------------------------
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

    last = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or data.get("last_input")
        or ""
    )

    if not last.strip():
        return jsonify({"status": "empty"})

    add_message(user_id, last, platform)

    return jsonify({"status": "received"})


@app.route("/")
def home():
    return "✅ ChatKit Workflow Proxy Running!"


if __name__ == "__main__":
    logger.info("🚀 Starting ChatKit proxy...")
    app.run(host="0.0.0.0", port=PORT)
