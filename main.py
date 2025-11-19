import os
import time
import threading
import logging
from flask import Flask, request, jsonify
import requests

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------------
# ENV
# -------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = "wf_691ac2e8aa388190a7b428f30a6ed0170545bfe71974583"
WORKFLOW_VERSION = "6"

MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
BATCH_WAIT_TIME = 2.0

# -------------------------
# Flask app
# -------------------------
app = Flask(__name__)

# -------------------------
# Temporary memory (no Mongo)
# -------------------------
pending_messages = {}
message_timers = {}
locks = {}
user_sessions = {}   # memory sessions

# -------------------------
# Send to ManyChat
# -------------------------
def send_reply(subscriber_id, text, platform):
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
                "messages": [
                    {"type": "text", "text": text.strip()}
                ]
            }
        },
        "channel": channel
    }

    try:
        r = requests.post(url, json=payload, headers=headers)
        r.raise_for_status()
        logger.info(f"ManyChat sent to {subscriber_id}")
    except:
        logger.exception("❌ ManyChat send failed")

# -------------------------
# Workflow REST Call
# -------------------------
def call_workflow(text, session_id):
    try:
        logger.info("Calling Workflow via REST...")

        url = f"https://api.openai.com/v1/workflows/{WORKFLOW_ID}/runs?version={WORKFLOW_VERSION}"

        payload = {
            "input": {
                "user_input": text,
                "session_id": session_id
            }
        }

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }

        r = requests.post(url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()

        resp = r.json()

        try:
            return resp["output"]["assistant_response"]
        except:
            return "لم أستطع استخراج رد الوكيل."

    except Exception:
        logger.exception("❌ Workflow REST failed")
        return "حصل خطأ أثناء تشغيل الوكيل."

# -------------------------
# Batch Processing
# -------------------------
def process_user(user_id):
    lock = locks.setdefault(user_id, threading.Lock())

    with lock:
        if user_id not in pending_messages:
            return

        session = user_sessions.get(user_id, {"platform": "facebook"})
        platform = session["platform"]
        texts = pending_messages[user_id]
        combined = "\n".join(texts).strip()

        logger.info(f"[{user_id}] Processing batch: {combined}")

        reply = call_workflow(combined, user_id)

        send_reply(user_id, reply, platform)

        pending_messages.pop(user_id, None)
        timer = message_timers.pop(user_id, None)
        if timer:
            timer.cancel()

        logger.info(f"[{user_id}] Done")

def add_to_queue(session, text):
    user_id = session["id"]

    if user_id in message_timers:
        message_timers[user_id].cancel()

    if user_id not in pending_messages:
        pending_messages[user_id] = []

    pending_messages[user_id].append(text)

    logger.info(f"Queued message for {user_id} (batch={len(pending_messages[user_id])})")

    t = threading.Timer(BATCH_WAIT_TIME, process_user, args=[user_id])
    message_timers[user_id] = t
    t.start()


# -------------------------
# Webhook
# -------------------------
@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    auth = request.headers.get("Authorization")
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact")

    if not contact:
        return jsonify({"error": "no-contact"}), 400

    user_id = str(contact["id"])
    platform = "instagram" if "instagram" in contact.get("source", "").lower() else "facebook"

    user_sessions[user_id] = {"id": user_id, "platform": platform}

    last_input = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or data.get("last_input")
    )

    if not last_input:
        return jsonify({"status": "no_input"})

    add_to_queue(user_sessions[user_id], last_input)

    return jsonify({"status": "received"})


@app.route("/")
def home():
    return "Workflow Bot Running"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
