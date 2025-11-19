import os
import json
import time
import threading
import logging
from datetime import datetime
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# -------------------------
# Setup
# -------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")  # wf_xxx
WORKFLOW_VERSION = os.getenv("WORKFLOW_VERSION")  # number only (5)
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", 2.0))

app = Flask(__name__)

pending = {}
timers = {}

# -------------------------
# ManyChat sender
# -------------------------
def send_mc(user_id, text):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "subscriber_id": str(user_id),
        "data": {
            "version": "v2",
            "content": {"messages": [{"type": "text", "text": text}]}
        }
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
        logging.info(f"ManyChat sent to {user_id}")
    except Exception:
        logging.exception("Failed sending to ManyChat")

# -------------------------
# Call Workflow (new API)
# -------------------------
def call_workflow(text, user_id):
    try:
        url = f"https://api.openai.com/v1/workflows/{WORKFLOW_ID}/runs?version={WORKFLOW_VERSION}"

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "input": {
                "user_message": text,
                "user_id": user_id
            }
        }

        logging.info("Calling Workflow REST...")
        r = requests.post(url, headers=headers, json=payload, timeout=40)
        r.raise_for_status()

        resp = r.json()

        # Extract response text
        output = resp.get("output", {})
        reply = output.get("assistant_reply", "⚠ خطأ: لم يتم العثور على ردّ من سير العمل.")

        return reply

    except Exception as e:
        logging.exception("Workflow REST failed")
        return "⚠ حصل خطأ أثناء تشغيل سير العمل."

# -------------------------
# Process batching
# -------------------------
def process_user(user_id):
    if user_id not in pending:
        return

    text = "\n".join(pending[user_id])
    logging.info(f"[{user_id}] Processing batch: {text}")

    reply = call_workflow(text, user_id)
    send_mc(user_id, reply)

    pending.pop(user_id, None)
    if user_id in timers:
        timers[user_id].cancel()
        timers.pop(user_id, None)


@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    auth = request.headers.get("Authorization")
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact")
    user_id = contact.get("id")

    last_input = (
        contact.get("last_text_input") or
        contact.get("last_input_text") or
        data.get("last_input")
    )

    if not last_input:
        return jsonify({"status": "no_input"})

    # Add to queue
    if user_id not in pending:
        pending[user_id] = []

    pending[user_id].append(last_input)
    logging.info(f"Queued message for {user_id} (batch={len(pending[user_id])})")

    # Reset timer
    if user_id in timers:
        timers[user_id].cancel()

    timers[user_id] = threading.Timer(BATCH_WAIT_TIME, process_user, args=[user_id])
    timers[user_id].start()

    return jsonify({"status": "received"})


@app.route("/")
def home():
    return "Workflow Bot Active"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logging.info(f"Running on port {port}")
    app.run(host="0.0.0.0", port=port)
