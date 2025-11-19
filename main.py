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
logger = logging.getLogger("workflow-proxy")

# --------- env ----------
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")       # must be sk-...
WORKFLOW_ID = os.getenv("WORKFLOW_ID")             # wf_xxx
WORKFLOW_VERSION = os.getenv("WORKFLOW_VERSION")   # e.g. 6
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
MONGO_URI = os.getenv("MONGO_URI", "").strip()     # optional
PORT = int(os.getenv("PORT", 5000))
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", 2.0))

# basic sanity
missing = [k for k in ("OPENAI_API_KEY", "WORKFLOW_ID", "WORKFLOW_VERSION", "MANYCHAT_API_KEY", "MANYCHAT_SECRET_KEY") if not globals().get(k)]
if missing:
    logger.critical(f"Missing env vars: {missing}. Fill .env and restart.")
    raise SystemExit(1)

# --------- optional mongo (safe if not provided) ----------
use_mongo = False
if MONGO_URI:
    try:
        from pymongo import MongoClient
        client_db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        db = client_db.get_database("multi_platform_bot")
        sessions_col = db.get_collection("sessions")
        use_mongo = True
        client_db.admin.command("ping")
        logger.info("✅ Connected to MongoDB")
    except Exception:
        logger.exception("Cannot connect to MongoDB — continuing without DB")
        use_mongo = False

# --------- app ----------
app = Flask(__name__)

# --------- batching state ----------
pending_messages = {}   # user_id -> {"texts": [], "session": session_doc}
message_timers = {}     # user_id -> Timer
processing_locks = {}   # user_id -> Lock

# --------- helpers ----------
def get_or_create_session(contact):
    user_id = str(contact.get("id"))
    if not user_id:
        return None
    now = datetime.now(timezone.utc)
    source = str(contact.get("source", "")).lower()
    platform = "Instagram" if "instagram" in source else "Facebook"

    session_doc = {
        "_id": user_id,
        "platform": platform,
        "profile": {
            "name": contact.get("name"),
            "profile_pic": contact.get("profile_pic")
        },
        "created": now,
        "last_contact_date": now
    }

    if use_mongo:
        doc = sessions_col.find_one({"_id": user_id})
        if doc:
            sessions_col.update_one(
                {"_id": user_id},
                {"$set": {
                    "last_contact_date": now,
                    "platform": platform,
                    "profile.name": contact.get("name"),
                    "profile.profile_pic": contact.get("profile_pic"),
                    "status": "active"
                }}
            )
            return sessions_col.find_one({"_id": user_id})
        sessions_col.insert_one(session_doc)
        return session_doc
    return session_doc


def send_manychat_reply(subscriber_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform == "Instagram" else "facebook"

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



# WALK JSON TO FIND COMMON KEYS
def find_first_key(obj, keys):
    if obj is None:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v is not None:
                return v
            res = find_first_key(v, keys)
            if res:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = find_first_key(item, keys)
            if res:
                return res
    return None



# --------- WORKFLOW — NEW CORRECT OPENAI ENDPOINT ----------
def call_workflow_via_rest(workflow_id, version, payload):
    """
    NEW OFFICIAL OpenAI Workflows API:
    POST https://api.openai.com/v1/workflows/runs
    """
    url = "https://api.openai.com/v1/workflows/runs"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    body = {
        "workflow_id": workflow_id,
        "input": payload.get("input", {})
    }

    # add version inside the body
    if version:
        try:
            body["version"] = int(version)
        except:
            pass

    try:
        r = requests.post(url, json=body, headers=headers, timeout=60)
        r.raise_for_status()
        return r.json()

    except requests.exceptions.HTTPError as e:
        logger.error(
            "Workflow REST failed: %s — body: %s",
            e, getattr(e.response, "text", None)
        )
        return {
            "__error": True,
            "status_code": getattr(e.response, "status_code", None),
            "text": getattr(e.response, "text", None)
        }

    except Exception:
        logger.exception("Workflow call exception")
        return {"__error": True, "exception": True}



# --------- PROCESSING ----------
def schedule_processing(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:

        if user_id not in pending_messages:
            return

        data = pending_messages[user_id]
        session_doc = data["session"]
        texts = data["texts"]
        combined = "\n".join(texts).strip()

        logger.info(f"Processing for {user_id}: {combined[:300]}")

        payload = {
            "input": {
                "text": combined,
                "full_contact": session_doc.get("raw_contact", {}),
                "timestamp": datetime.utcnow().isoformat()
            }
        }

        resp_json = call_workflow_via_rest(WORKFLOW_ID, WORKFLOW_VERSION, payload)

        if isinstance(resp_json, dict) and resp_json.get("__error"):
            reply_text = "⚠ حدث خطأ أثناء معالجة الطلب (workflow)."
            logger.error("Workflow returned error payload: %s", resp_json)

        else:
            reply_text = find_first_key(
                resp_json,
                ["reply_text", "reply", "text", "output_text", "message"]
            )

            if isinstance(reply_text, dict):
                reply_text = find_first_key(reply_text, ["text", "value"])

            if not reply_text:
                try:
                    reply_text = json.dumps(resp_json)[:2000]
                except:
                    reply_text = "⚠ لم أتمكن من استخراج ردّ الوكيل."

        send_manychat_reply(user_id, reply_text, platform=session_doc.get("platform", "Facebook"))

        pending_messages.pop(user_id, None)
        t = message_timers.pop(user_id, None)
        if t:
            try:
                t.cancel()
            except:
                pass

        logger.info(f"Finished processing {user_id}")



def add_to_queue(session_doc, text, raw_contact=None):
    user_id = session_doc["_id"]

    if user_id in message_timers:
        try:
            message_timers[user_id].cancel()
        except:
            pass

    if user_id not in pending_messages:
        session_doc["raw_contact"] = raw_contact or {}
        pending_messages[user_id] = {"texts": [], "session": session_doc}

    pending_messages[user_id]["texts"].append(text)
    logger.info(f"Queued message for {user_id}; batch size {len(pending_messages[user_id]['texts'])}")

    timer = threading.Timer(BATCH_WAIT_TIME, schedule_processing, args=[user_id])
    message_timers[user_id] = timer
    timer.start()



# --------- WEBHOOK ----------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():
    auth = request.headers.get("Authorization")
    if not MANYCHAT_SECRET_KEY or auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        logger.warning("Unauthorized webhook call")
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact")
    if not contact:
        return jsonify({"error": "invalid"}), 400

    session_doc = get_or_create_session(contact)
    if not session_doc:
        return jsonify({"error": "session_failed"}), 500

    last_input = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or data.get("last_input")
        or ""
    )

    if not str(last_input).strip():
        return jsonify({"status": "no_input"})

    add_to_queue(session_doc, str(last_input), raw_contact=contact)
    return jsonify({"status": "received"})


@app.route("/")
def home():
    return "✅ Workflow proxy running"


if __name__ == "__main__":
    logger.info("Starting app")
    app.run(host="0.0.0.0", port=PORT)
