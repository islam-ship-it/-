#!/usr/bin/env python3
# main.py - No Mongo version: in-memory sessions, batching, Workflow call (SDK or REST fallback)
import os
import time
import json
import threading
import logging
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests

# try to import OpenAI client object
try:
    from openai import OpenAI
    HAS_OPENAI_SDK = True
except Exception:
    HAS_OPENAI_SDK = False

# -------------------------
# Logging & env
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")        # wf_xxx
WORKFLOW_VERSION = os.getenv("WORKFLOW_VERSION", "")  # e.g. "5" or empty to omit
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", 2.0))

required = {
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "WORKFLOW_ID": WORKFLOW_ID,
    "MANYCHAT_API_KEY": MANYCHAT_API_KEY,
    "MANYCHAT_SECRET_KEY": MANYCHAT_SECRET_KEY
}
missing = [k for k, v in required.items() if not v]
if missing:
    logger.critical(f"Missing env vars: {missing}. Fill .env or set in Render and restart.")
    raise SystemExit(1)

# -------------------------
# OpenAI client (if SDK available)
# -------------------------
client = None
if HAS_OPENAI_SDK:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("OpenAI SDK available and client initialized.")
    except Exception:
        client = None
        logger.warning("OpenAI SDK import succeeded but client init failed. Will use REST fallback.")

# -------------------------
# Flask app
# -------------------------
app = Flask(__name__)

# -------------------------
# In-memory sessions/messages (no Mongo)
# Structure:
#   sessions[user_id] = { "platform": "...", "profile": {...}, "history": [ {role,text,ts}, ... ] }
# -------------------------
sessions = {}
sessions_lock = threading.Lock()

# -------------------------
# Batching state
# -------------------------
pending_messages = {}   # user_id -> {"texts": [], "session_id": user_id}
message_timers = {}     # user_id -> Timer
processing_locks = {}   # user_id -> Lock

# -------------------------
# Helpers
# -------------------------
def get_or_create_session(contact):
    # contact is ManyChat full_contact
    user_id = str(contact.get("id") or contact.get("subscriber_id") or "")
    if not user_id:
        return None
    with sessions_lock:
        doc = sessions.get(user_id)
        now = datetime.now(timezone.utc).isoformat()
        source = str(contact.get("source", "")).lower()
        platform = "Instagram" if "instagram" in source else "Facebook"
        if doc:
            doc["last_contact_date"] = now
            doc["platform"] = platform
            doc["profile"]["name"] = contact.get("name")
            doc["profile"]["profile_pic"] = contact.get("profile_pic")
            return doc
        # create
        new_doc = {
            "_id": user_id,
            "platform": platform,
            "profile": {
                "name": contact.get("name"),
                "profile_pic": contact.get("profile_pic")
            },
            "history": [],  # store simple chat history to include in workflow input
            "created": now,
            "last_contact_date": now
        }
        sessions[user_id] = new_doc
        return new_doc

def save_message_locally(user_id, role, text):
    with sessions_lock:
        s = sessions.get(user_id)
        if not s:
            return
        s["history"].append({
            "role": role,
            "text": text,
            "ts": datetime.utcnow().isoformat()
        })
        # keep history bounded to avoid huge payloads
        MAX_HISTORY = 30
        if len(s["history"]) > MAX_HISTORY:
            s["history"] = s["history"][-MAX_HISTORY:]

# -------------------------
# ManyChat sender
# -------------------------
def send_manychat_reply(subscriber_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }
    channel = "instagram" if (platform and platform.lower() == "instagram") else "facebook"
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

# -------------------------
# Workflow call helpers
# We'll try SDK first (client.workflows.runs.create or client.agents.responses.create),
# otherwise we call REST endpoint for Workflows v1: POST /v1/workflows/{workflow_id}/runs?version=...
# -------------------------
def call_workflow_via_rest(workflow_id, version, input_obj):
    """
    POST to /v1/workflows/{workflow_id}/runs
    input_obj is a dict that will be sent under "input".
    """
    url = f"https://api.openai.com/v1/workflows/{workflow_id}/runs"
    if version:
        url = f"{url}?version={version}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    body = {"input": input_obj}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.exception("Workflow REST call failed")
        raise

def extract_text_from_workflow_response(resp_json):
    """
    Different workflow outputs may have different shapes. Try common patterns:
    - resp_json["output"] or ["outputs"] or top-level "result" etc.
    - fallback to stringify whole response.
    """
    try:
        # common: resp_json.get("result", {}) or resp_json.get("output")
        # We'll try to search recursively for text fields.
        texts = []

        def walk(obj):
            if obj is None:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("text", "output_text", "content", "value"):
                        if isinstance(v, str):
                            texts.append(v)
                        elif isinstance(v, list):
                            for it in v:
                                if isinstance(it, str):
                                    texts.append(it)
                                else:
                                    walk(it)
                    else:
                        walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)
            # else ignore

        walk(resp_json)
        if texts:
            return "\n".join(texts).strip()
        # fallback: try common keys
        for k in ("output_text", "output", "result", "response"):
            if k in resp_json:
                v = resp_json[k]
                if isinstance(v, str):
                    return v
                elif isinstance(v, dict):
                    return json.dumps(v)
        return json.dumps(resp_json)[:4000]
    except Exception:
        logger.exception("Failed to parse workflow response")
        try:
            return json.dumps(resp_json)[:4000]
        except Exception:
            return "حصل خطأ في استخراج الرد من الـ Workflow."

def call_workflow(workflow_id, version, session_doc, user_text):
    """
    Tries:
      1) SDK: client.workflows.runs.create if available
      2) REST fallback
    We pass:
      - input.history (short recent chat)
      - input.latest_user_message
      - input.manychat_meta (profile/platform)
      - input.use_file_search_hint: True (so that workflow can decide to call file-search tool)
    """
    input_payload = {
        "latest_user_message": str(user_text),
        "history": session_doc.get("history", []),
        "profile": session_doc.get("profile", {}),
        "platform": session_doc.get("platform", ""),
        "use_file_search_hint": True
    }

    # 1) SDK attempt (if available)
    if client:
        try:
            # try workflows API on SDK (some SDK versions expose workflows)
            if hasattr(client, "workflows") and hasattr(client.workflows, "runs"):
                logger.info("Calling workflow via OpenAI SDK (workflows.runs.create)")
                run_args = {"workflow_id": workflow_id, "input": input_payload}
                if version:
                    run_args["version"] = version
                resp = client.workflows.runs.create(**run_args)
                # resp may be object or dict
                if isinstance(resp, dict):
                    return extract_text_from_workflow_response(resp)
                else:
                    # try object attributes
                    try:
                        return getattr(resp, "output_text", str(resp))
                    except Exception:
                        return str(resp)
            # fallback: some SDKs support agents.responses - try that (if agent usage)
            if hasattr(client, "agents") and hasattr(client.agents, "responses"):
                logger.info("Calling agent.responses.create via SDK as fallback")
                resp = client.agents.responses.create(agent_id=WORKFLOW_ID, input=input_payload)
                if isinstance(resp, dict):
                    return extract_text_from_workflow_response(resp)
                else:
                    return getattr(resp, "output_text", str(resp))
        except Exception:
            logger.exception("SDK workflow/agent call failed, will fallback to REST")

    # 2) REST fallback
    logger.info("Calling workflow via REST fallback")
    resp_json = call_workflow_via_rest(workflow_id, version, input_payload)
    return extract_text_from_workflow_response(resp_json)

# -------------------------
# Batching & processing
# -------------------------
def schedule_processing(user_id):
    lock = processing_locks.setdefault(user_id, threading.Lock())
    with lock:
        if user_id not in pending_messages:
            return
        data = pending_messages[user_id]
        session_doc = data["session"]
        texts = data["texts"]
        combined = "\n".join(texts).strip()
        logger.info(f"[{user_id}] Processing batch: {combined[:300]}")

        # save locally
        save_message_locally(user_id, "user", combined)

        # call workflow
        try:
            reply = call_workflow(WORKFLOW_ID, WORKFLOW_VERSION, session_doc, combined)
        except Exception:
            logger.exception("Workflow call failed")
            reply = "حصل خطأ أثناء معالجة الطلب. حاول مرة ثانية."

        # save bot reply locally
        save_message_locally(user_id, "assistant", reply)

        # send to ManyChat
        send_manychat_reply(user_id, reply, platform=session_doc.get("platform", "facebook"))

        # cleanup
        pending_messages.pop(user_id, None)
        t = message_timers.pop(user_id, None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass
        logger.info(f"[{user_id}] Finished processing")

def add_to_queue(session_doc, text):
    user_id = session_doc["_id"]
    # cancel existing timer if any
    if user_id in message_timers:
        try:
            message_timers[user_id].cancel()
        except Exception:
            pass

    if user_id not in pending_messages:
        pending_messages[user_id] = {"texts": [], "session": session_doc}

    pending_messages[user_id]["texts"].append(str(text))
    logger.info(f"Queued message for {user_id} (batch size={len(pending_messages[user_id]['texts'])})")
    timer = threading.Timer(BATCH_WAIT_TIME, schedule_processing, args=[user_id])
    message_timers[user_id] = timer
    timer.start()

# -------------------------
# ManyChat webhook
# -------------------------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():
    auth = request.headers.get("Authorization")
    if not MANYCHAT_SECRET_KEY or auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        logger.warning("Unauthorized webhook call")
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact")
    if not contact:
        logger.error("full_contact missing")
        return jsonify({"error": "invalid"}), 400

    session_doc = get_or_create_session(contact)
    if not session_doc:
        return jsonify({"error": "session_failed"}), 500

    last_input = contact.get("last_text_input") or contact.get("last_input_text") or data.get("last_input")
    if not last_input or str(last_input).strip() == "":
        return jsonify({"status": "no_input"})

    add_to_queue(session_doc, last_input)
    return jsonify({"status": "received"})

# -------------------------
# Health
# -------------------------
@app.route("/")
def home():
    return "Workflow Bot (no Mongo) — Running"

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
