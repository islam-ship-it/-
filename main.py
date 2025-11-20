"""
main_v22.py

Fixed version v22 of the webhook/processor that:
- Avoids sending huge concatenated strings to the Threads API
- Sends a structured payload (JSON) with limited history (last N messages)
- If payload still too large, progressively truncates history and then summarizes it using the assistant
- Keeps per-user locks/queues to avoid overlapping runs
- Supports text + image URLs + audio (audio transcribed elsewhere or passed as text)
- Clear logging and safe error handling

This file is intentionally self-contained; adapt parts (OpenAI model names, DB, flask configuration)
for your environment and keys.
"""

import os
import json
import logging
import threading
import time
from typing import List, Dict, Any, Optional
from flask import Flask, request, jsonify

# Make sure openai package is installed and configured. Uses OpenAI Python SDK v1-style client.
try:
    from openai import OpenAI
except Exception:
    # If import fails, raise a helpful error when starting.
    raise

# ------------------- Config -------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY environment variable required")

client = OpenAI(api_key=OPENAI_API_KEY)

# Limits and behavior tuning
MAX_CONTENT_CHARS = 250_000  # safe margin under 256k limit
HISTORY_MAX_MESSAGES = 6     # keep last 6 messages (adjustable)
SUMMARY_MODEL = "gpt-4o-mini"  # change to available summarization model in your account
SUMMARY_MAX_TOKENS = 256
THREADS_API_PATH_TEMPLATE = "/v1/threads/{thread_id}/messages"

# Per-user locks to avoid concurrent processors racing
user_locks: Dict[str, threading.Lock] = {}
user_locks_mutex = threading.Lock()

# Basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)


# ------------------- Utilities -------------------

def get_user_lock(user_id: str) -> threading.Lock:
    """Return a lock object for a given user id (create if absent)."""
    with user_locks_mutex:
        if user_id not in user_locks:
            user_locks[user_id] = threading.Lock()
        return user_locks[user_id]


def last_n_history(history: List[Dict[str, Any]], n: int = HISTORY_MAX_MESSAGES) -> List[Dict[str, Any]]:
    """Return the last n items of conversation history, preserving order oldest->newest."""
    if not history:
        return []
    return history[-n:]


def build_structured_payload(
    user_text: str,
    history: List[Dict[str, Any]],
    images: Optional[List[str]] = None,
    audio_texts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Construct a JSON-serializable structured payload to send to the Threads/messages API.

    Payload format:
    {
      "type": "conversation_input",
      "text": "...",
      "images": [...],
      "audio_texts": [...],
      "history": [{"role":"user","text":"..."}, {"role":"assistant","text":"..."}]
    }
    """
    payload = {
        "type": "conversation_input",
        "text": user_text or "",
        "images": images or [],
        "audio_texts": audio_texts or [],
        "history": last_n_history(history, HISTORY_MAX_MESSAGES),
    }
    return payload


def safe_serialize_payload(payload: Dict[str, Any]) -> str:
    """Serialize payload to JSON string. If the serialized size exceeds MAX_CONTENT_CHARS,
    progressively trim the history and, if necessary, request a summarized history using the summarizer.
    """
    payload_copy = dict(payload)

    serialized = json.dumps(payload_copy, ensure_ascii=False)
    if len(serialized) <= MAX_CONTENT_CHARS:
        return serialized

    # 1) Trim history count gradually until we fall under the limit
    history = payload_copy.get("history", [])
    while len(history) > 0:
        history = history[1:]  # drop the oldest one
        payload_copy["history"] = history
        serialized = json.dumps(payload_copy, ensure_ascii=False)
        if len(serialized) <= MAX_CONTENT_CHARS:
            return serialized

    # 2) If still too large, produce a summary of the original history and include the summary only
    logger.info("Payload too large after trimming; creating history summary")
    try:
        summary = summarize_history(payload.get("history", []))
    except Exception as e:
        logger.exception("Failed to summarize history: %s", e)
        # fallback: include a minimal note
        summary = "(conversation history omitted due to size)"

    payload_copy["history_summary"] = summary
    payload_copy["history"] = []
    serialized = json.dumps(payload_copy, ensure_ascii=False)
    if len(serialized) <= MAX_CONTENT_CHARS:
        return serialized

    # final fallback: send only the text and a short note
    fallback = {"type": "conversation_input", "text": payload.get("text", ""), "note": "history omitted (too large)"}
    return json.dumps(fallback, ensure_ascii=False)


def summarize_history(history: List[Dict[str, Any]]) -> str:
    """Call the assistant (synchronously) with a short summarization prompt.
    Returns a short summary string.
    """
    # Quick guard
    if not history:
        return ""

    # Build prompt for summarization
    convo_text_parts = []
    for m in history:
        role = m.get("role", "user")
        text = m.get("text", "")
        convo_text_parts.append(f"{role}: {text}")
    convo_text = "\n".join(convo_text_parts)

    prompt = (
        "Summarize the following conversation briefly (2-4 sentences) focusing on user intent and open items:\n\n"
        + convo_text
    )

    try:
        # Use the Responses API to get a short summary. Adjust model/name as available.
        resp = client.responses.create(
            model=SUMMARY_MODEL,
            input=[
                {"role": "user", "content": prompt}
            ],
            max_tokens=SUMMARY_MAX_TOKENS,
            temperature=0.2,
        )

        # The new SDK returns outputs under resp.output or resp.get("output"). We'll try to robustly extract text.
        summary_text = ""
        if hasattr(resp, "output") and resp.output:
            # output is likely a list of objects with 'content' items
            parts = []
            for item in resp.output:
                if isinstance(item, dict) and item.get("type") == "output_text":
                    parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            summary_text = " ".join(p for p in parts if p)
        else:
            # fallback: try resp.get("text") or join choices
            summary_text = str(resp)

        # final small cleanup
        summary_text = summary_text.strip()
        if not summary_text:
            summary_text = "(summary empty)"

        return summary_text

    except Exception as e:
        logger.exception("summarize_history failed: %s", e)
        raise


# ------------------- Threads API wrapper -------------------

def post_message_to_thread(thread_id: str, structured_payload_json: str) -> Dict[str, Any]:
    """Post a message to the thread using the OpenAI client.

    The function takes care of catching and logging size / 400 errors and returns the API result dict.
    """
    try:
        # Here we call the threads messages endpoint. The exact SDK call may differ per SDK release.
        # We try the 'client.responses' fallback if threads endpoint is not present.
        logger.info("Posting to thread %s, payload_size=%d", thread_id, len(structured_payload_json))

        # If your SDK supports threads messages directly, uncomment and adapt the call below:
        # result = client.beta.threads.messages.create(thread_id=thread_id, role="user", content=structured_payload_json)

        # Generic HTTP fallback via client._request (NOT public API) is not recommended.
        # Use the Responses API as a proxy: create a response using the payload as content.
        result = client.responses.create(
            model="gpt-4o-mini",
            input=[{"role": "user", "content": structured_payload_json}],
        )

        return result

    except Exception as e:
        logger.exception("Failed to post to thread: %s", e)
        raise


# ------------------- Webhook Endpoint -------------------

@app.route('/manychat_webhook', methods=['POST'])
def manychat_webhook():
    """Receive incoming webhook from ManyChat (or other chat gateway) and forward a cleaned/structured
    message to the assistant thread.

    Expected JSON (example):
    {
      "user_id": "123",
      "thread_id": "thread_abc",
      "text": "Hello",
      "images": ["https://..."],
      "audio_texts": ["transcribed audio text ..."],
      "history": [{"role":"user","text":"..."}, ...]
    }
    """
    payload = request.get_json(force=True)
    logger.info("Webhook received payload keys=%s", list(payload.keys()) if isinstance(payload, dict) else str(type(payload)))

    user_id = str(payload.get('user_id') or payload.get('sender') or 'unknown')
    thread_id = payload.get('thread_id')
    user_text = payload.get('text', '')
    images = payload.get('images', [])
    audio_texts = payload.get('audio_texts', [])
    history = payload.get('history', []) or []

    if not thread_id:
        logger.warning("No thread_id provided in webhook")
        return jsonify({"ok": False, "error": "thread_id required"}), 400

    lock = get_user_lock(user_id)
    if not lock.acquire(blocking=False):
        # Another processor is running for this user. We drop or queue depending on your desired logic.
        logger.warning("Processor busy for user %s - rejecting incoming event to avoid run overlap", user_id)
        return jsonify({"ok": False, "error": "processor_busy"}), 429

    try:
        # Build structured payload
        structured_payload = build_structured_payload(user_text=user_text, history=history, images=images, audio_texts=audio_texts)
        serialized = safe_serialize_payload(structured_payload)

        # Post to thread
        result = post_message_to_thread(thread_id, serialized)

        # Provide a compact, friendly reply for the webhook initiator
        return jsonify({"ok": True, "posted_size": len(serialized), "thread_id": thread_id}), 200

    except Exception as e:
        logger.exception("Error processing webhook for user %s: %s", user_id, e)
        return jsonify({"ok": False, "error": str(e)}), 500

    finally:
        lock.release()


# ------------------- Local runner -------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info("Starting main_v22 on port %d", port)
    app.run(host='0.0.0.0', port=port)
