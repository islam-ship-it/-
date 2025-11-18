import os
import time
import json
import requests
import threading
import logging
from flask import Flask, request, jsonify
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv

# --------------------- logging & env ---------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

load_dotenv()
logger.info("▶️ [START] Environment Loaded.")

# --------------------- config / env vars ---------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")            # wf_xxx
WORKFLOW_VERSION = os.getenv("WORKFLOW_VERSION")  # e.g. "4" or "version":"4"
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

required = {
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "WORKFLOW_ID": WORKFLOW_ID,
    "MONGO_URI": MONGO_URI,
    "MANYCHAT_API_KEY": MANYCHAT_API_KEY,
    "MANYCHAT_SECRET_KEY": MANYCHAT_SECRET_KEY
}
missing = [k for k, v in required.items() if not v]
if missing:
    logger.critical(f"❌ Missing required env vars: {missing}")
    raise SystemExit(1)

# --------------------- Mongo ---------------------
try:
    client_db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    messages_collection = db["messages"]  # for full conversation storage (optional)
    # quick connectivity check
    client_db.admin.command("ping")
    logger.info("✅ [DB] Connected to MongoDB successfully.")
except Exception as e:
    logger.critical(f"❌ [DB] Failed to connect: {e}", exc_info=True)
    raise

# --------------------- Flask app ---------------------
app = Flask(__name__)

# --------------------- batching state ---------------------
pending_messages = {}   # user_id -> {"texts": [...], "session": {...}}
message_timers = {}     # user_id -> Timer
processing_locks = {}   # user_id -> Lock
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", 2.0))

# --------------------- helpers: sessions ---------------------
def get_or_create_session(contact_data):
    user_id = str(contact_data.get("id")) if contact_data.get("id") is not None else None
    if not user_id:
        logger.error(f"❌ [SESSION] contact_data missing id: {contact_data}")
        return None

    session = sessions_collection.find_one({"_id": user_id})
    now = datetime.now(timezone.utc)

    source = str(contact_data.get("source", "")).lower()
    if "instagram" in source:
        platform = "Instagram"
    elif "facebook" in source:
        platform = "Facebook"
    else:
        platform = "Facebook"

    if session:
        sessions_collection.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact_date": now,
                "platform": platform,
                "profile.name": contact_data.get("name"),
                "profile.profile_pic": contact_data.get("profile_pic"),
                "status": "active"
            }}
        )
        return sessions_collection.find_one({"_id": user_id})

    new_session = {
        "_id": user_id,
        "platform": platform,
        "profile": {
            "name": contact_data.get("name"),
            "profile_pic": contact_data.get("profile_pic"),
        },
        "created": now,
        "last_contact_date": now,
        "status": "active",
    }
    sessions_collection.insert_one(new_session)
    logger.info(f"🆕 [SESSION] Created new session for user {user_id}")
    return new_session

# --------------------- helpers: save messages ---------------------
def save_message(user_id, sender, text):
    try:
        messages_collection.insert_one({
            "user_id": user_id,
            "sender": sender,
            "message": text,
            "timestamp": datetime.utcnow()
        })
    except Exception as e:
        logger.exception(f"[DB] Failed saving message for {user_id}: {e}")

# --------------------- ManyChat sender ---------------------
def send_manychat_reply(subscriber_id, text, platform):
    if not MANYCHAT_API_KEY:
        logger.error("❌ [SENDER] MANYCHAT_API_KEY missing.")
        return False

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }
    channel = "instagram" if platform == "Instagram" else "facebook"
    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text.strip()}]}},
        "channel": channel
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
        logger.info(f"📤 [SENDER] Sent to {subscriber_id} on {channel}")
        return True
    except requests.exceptions.HTTPError as e:
        body = e.response.text if getattr(e, "response", None) is not None else str(e)
        logger.error(f"❌ [SENDER] HTTPError: {e} | body: {body}")
    except Exception as e:
        logger.exception(f"❌ [SENDER] Failed send_manychat_reply: {e}")
    return False

# --------------------- Workflow runner (sync HTTP) ---------------------
def extract_workflow_reply(resp_json):
    """
    Try multiple possible response shapes to extract a sensible text reply.
    """
    if not isinstance(resp_json, dict):
        return str(resp_json)

    # Common keys that may hold text
    # 1) 'output_text'
    if "output_text" in resp_json and isinstance(resp_json["output_text"], str):
        return resp_json["output_text"]

    # 2) 'output' could be string or list/dict
    out = resp_json.get("output")
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        # try join text fields
        parts = []
        for item in out:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # try common patterns
                if "text" in item and isinstance(item["text"], str):
                    parts.append(item["text"])
                elif "content" in item:
                    # content may be list of chunks
                    if isinstance(item["content"], list):
                        for c in item["content"]:
                            if isinstance(c, dict) and "text" in c:
                                parts.append(c["text"])
        if parts:
            return "\n".join(parts)

    # 3) maybe 'result' or 'response' keys
    for k in ("result", "response", "data", "run", "results"):
        v = resp_json.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            # try nested output_text
            txt = v.get("output_text") or v.get("text") or v.get("reply")
            if isinstance(txt, str):
                return txt

    # 4) fallback: pretty-print json
    return json.dumps(resp_json, ensure_ascii=False)

def run_workflow_sync(text, session):
    """
    Call the Agent Builder Workflow runs endpoint synchronously and return the extracted reply text.
    """
    workflow_id = WORKFLOW_ID
    version = WORKFLOW_VERSION  # may be None

    url = f"https://api.openai.com/v1/workflows/{workflow_id}/runs"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "input": {
            # The schema your workflow expects — many agent-builder workflows accept "input_as_text"
            "input_as_text": text
        }
    }
    # include version if provided
    if version:
        # if version already looks like 'version="4"' let user supply exact; else add version field
        # common simple case: version="4" or "4"
        if isinstance(version, str) and version.strip().startswith("version"):
            # try parse 'version="4"' -> include as is in payload top-level
            # but simplest: include top-level "version" key with digits
            # fallback: include version string as provided
            payload["version"] = version
        else:
            payload["version"] = version

    try:
        logger.info(f"🚀 [WORKFLOW] Calling Workflow {workflow_id} with input len={len(text)}")
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        resp_json = r.json()
        logger.debug(f"[WORKFLOW] Raw response: {json.dumps(resp_json)[:1000]}")
        reply = extract_workflow_reply(resp_json)
        return reply
    except requests.exceptions.HTTPError as e:
        body = e.response.text if getattr(e, "response", None) is not None else str(e)
        logger.error(f"❌ [WORKFLOW] HTTPError calling workflow: {e} | body: {body}")
        # try to surface a friendly message for user
        return "⚠️ عفوًا، حصل خطأ أثناء التواصل مع الوكيل. حاول مرة تانية."
    except Exception as e:
        logger.exception(f"❌ [WORKFLOW] Unexpected error: {e}")
        return "⚠️ عفوًا، حدث خطأ غير متوقع أثناء معالجة طلبك."

# --------------------- processing / queue ---------------------
def schedule_message_processing(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages:
            return

        data = pending_messages[user_id]
        session = data["session"]
        combined = "\n".join(data["texts"]).strip()
        logger.info(f"🔍 [PROCESS] Processing batch for {user_id}: {combined[:300]}")

        # Save user combined as a single message in DB (optional)
        try:
            save_message(user_id, "user", combined)
        except Exception:
            pass

        # Call workflow synchronously
        reply = run_workflow_sync(combined, session)

        # Save bot reply to DB
        try:
            save_message(user_id, "bot", reply)
        except Exception:
            pass

        # Send via ManyChat
        send_manychat_reply(user_id, reply, platform=session.get("platform", "Facebook"))

        # cleanup
        pending_messages.pop(user_id, None)
        timer = message_timers.pop(user_id, None)
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass

        logger.info(f"✅ [PROCESS] Finished processing for {user_id}")

def add_to_queue(session, text):
    user_id = session["_id"]
    if user_id in message_timers:
        try:
            message_timers[user_id].cancel()
        except Exception:
            pass
        logger.debug(f"⏳ [QUEUE] Cancelled existing timer for {user_id}")

    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session}
    pending_messages[user_id]["texts"].append(text)
    logger.info(f"➕ [QUEUE] Added message for {user_id}. Batch size: {len(pending_messages[user_id]['texts'])}")

    timer = threading.Timer(BATCH_WAIT_TIME, schedule_message_processing, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

# --------------------- webhook ---------------------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    logger.info("📞 [WEBHOOK] Received request")
    auth_header = request.headers.get('Authorization')
    if not MANYCHAT_SECRET_KEY or auth_header != f'Bearer {MANYCHAT_SECRET_KEY}':
        logger.critical("🚨 [WEBHOOK] Unauthorized attempt")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact")
    if not contact:
        logger.error("❌ [WEBHOOK] full_contact missing in payload")
        return jsonify({"status": "error", "message": "Invalid data"}), 400

    session = get_or_create_session(contact)
    if not session:
        return jsonify({"status": "error", "message": "Failed to create session"}), 500

    last_input = contact.get("last_text_input") or contact.get("last_input_text") or data.get("last_input")
    if not last_input or str(last_input).strip() == "":
        logger.warning("[WEBHOOK] No input text found")
        return jsonify({"status": "no_input"})

    add_to_queue(session, last_input)
    logger.info("[WEBHOOK] Queued for processing")
    return jsonify({"status": "received"})

# --------------------- simple health ---------------------
@app.route("/")
def home():
    return "✅ Bot Running — Workflow integration active"

# --------------------- run app ---------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Starting Flask on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
