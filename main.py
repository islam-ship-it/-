# main.py  — Async refactor (v2)
import os
import asyncio
import logging
import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional, Deque, List

import aiohttp
from aiohttp import ClientError
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from pymongo import MongoClient
from prometheus_client import start_http_server, Counter, Histogram

# Optional: if you use OpenAI official client, we'll call blocking parts in threadpool
from openai import OpenAI

# -------------------------
# Config & Logging
# -------------------------
load_dotenv()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("async-bot")

# hide secrets from accidental logs (helper)
def mask_secret(value: Optional[str]) -> str:
    if not value: return "<missing>"
    return value[:4] + "..." + value[-4:]

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", "2.0"))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
OPENAI_RUN_TIMEOUT = int(os.getenv("OPENAI_RUN_TIMEOUT", "90"))
MANYCHAT_SEND_TIMEOUT = int(os.getenv("MANYCHAT_SEND_TIMEOUT", "20"))

logger.info(f"Config: BATCH_WAIT_TIME={BATCH_WAIT_TIME}, MAX_HISTORY_MESSAGES={MAX_HISTORY_MESSAGES}")
logger.info(f"Loaded keys: OPENAI_API_KEY={mask_secret(OPENAI_API_KEY)}, MANYCHAT_API_KEY={mask_secret(MANYCHAT_API_KEY)}")

# -------------------------
# DB init
# -------------------------
if not MONGO_URI:
    logger.critical("MONGO_URI missing - exiting")
    raise SystemExit(1)
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("Connected to MongoDB")
except Exception as e:
    logger.critical("Failed to connect to MongoDB", exc_info=True)
    raise

# -------------------------
# OpenAI client (blocking) - we'll call via asyncio.to_thread
# -------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# Prometheus metrics (basic)
# -------------------------
start_http_server(8001)  # runs in background thread
MET_requests = Counter("bot_requests_total", "Total incoming webhook requests")
MET_processed = Counter("bot_processed_total", "Total processed batches")
MET_errors = Counter("bot_errors_total", "Total errors")
MET_response_time = Histogram("bot_response_seconds", "Time to get assistant response")

# -------------------------
# Async structures
# -------------------------
user_queues: defaultdict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
user_processors: dict[str, asyncio.Task] = {}
user_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
app = Flask(__name__)

# -------------------------
# Utilities: async retry with exponential backoff
# -------------------------
async def async_retry(func, *args, retries=4, initial_delay=0.5, factor=2.0, **kwargs):
    delay = initial_delay
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            logger.warning(f"Attempt {attempt} failed for {func.__name__}: {e}")
            if attempt == retries:
                break
            await asyncio.sleep(delay)
            delay *= factor
    raise last_exc

# -------------------------
# ManyChat send (async)
# -------------------------
async def manychat_send(subscriber_id: str, text_message: str, platform: str):
    if not MANYCHAT_API_KEY:
        logger.error("MANYCHAT_API_KEY missing")
        raise RuntimeError("MANYCHAT_API_KEY missing")

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform.lower() == "instagram" else "facebook"
    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message.strip()}]}}
    }
    params = {"channel": channel}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, params=params, json=payload, timeout=MANYCHAT_SEND_TIMEOUT) as resp:
            text = await resp.text()
            if resp.status >= 400:
                logger.error(f"ManyChat send failed status={resp.status} body={text}")
                raise ClientError(f"ManyChat error {resp.status}: {text}")
            logger.info(f"Sent to ManyChat {subscriber_id}@{channel}")

# -------------------------
# OpenAI wrappers (blocking calls wrapped in to_thread)
# -------------------------
async def create_thread() -> str:
    def _create():
        return client.beta.threads.create()
    thread = await asyncio.to_thread(_create)
    # thread.id assumed
    return getattr(thread, "id", None)

async def list_thread_messages(thread_id: str, limit: int = 20):
    def _list():
        return client.beta.threads.messages.list(thread_id=thread_id, limit=limit)
    res = await asyncio.to_thread(_list)
    return res

async def create_thread_message(thread_id: str, role: str, content: str):
    def _create():
        return client.beta.threads.messages.create(thread_id=thread_id, role=role, content=content)
    return await asyncio.to_thread(_create)

async def create_thread_run(thread_id: str, assistant_id: str):
    def _create():
        return client.beta.threads.runs.create(thread_id=thread_id, assistant_id=assistant_id)
    return await asyncio.to_thread(_create)

async def retrieve_run(thread_id: str, run_id: str):
    def _retrieve():
        return client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run_id)
    return await asyncio.to_thread(_retrieve)

# -------------------------
# Session utilities
# -------------------------
def get_or_create_session_from_contact_sync(contact_data: dict):
    # This runs sync (used inside webhook which is sync), minimal DB logic
    user_id = str(contact_data.get("id")) if contact_data.get("id") is not None else None
    if not user_id:
        logger.error("No contact id in payload")
        return None
    session = sessions_collection.find_one({"_id": user_id})
    now_utc = datetime.now(timezone.utc)

    contact_source = (contact_data.get("source") or "").lower()
    if "instagram" in contact_source or contact_data.get("ig_id"):
        main_platform = "Instagram"
    elif "facebook" in contact_source:
        main_platform = "Facebook"
    else:
        main_platform = "Facebook"

    if session:
        update_fields = {
            "last_contact_date": now_utc,
            "platform": main_platform,
            "profile.name": contact_data.get("name"),
            "profile.profile_pic": contact_data.get("profile_pic"),
            "status": "active"
        }
        sessions_collection.update_one({"_id": user_id}, {"$set": {k: v for k, v in update_fields.items() if v is not None}})
        return sessions_collection.find_one({"_id": user_id})
    else:
        new_session = {
            "_id": user_id,
            "platform": main_platform,
            "profile": {
                "name": contact_data.get("name"),
                "first_name": contact_data.get("first_name"),
                "last_name": contact_data.get("last_name"),
                "profile_pic": contact_data.get("profile_pic")
            },
            "openai_thread_id": None,
            "tags": [f"source:{main_platform.lower()}"],
            "custom_fields": contact_data.get("custom_fields", {}),
            "conversation_summary": "",
            "status": "active",
            "first_contact_date": now_utc,
            "last_contact_date": now_utc
        }
        sessions_collection.insert_one(new_session)
        return new_session

# -------------------------
# Conversation summarization (async)
# -------------------------
async def summarize_and_save_conversation(user_id: str, thread_id: str):
    logger.info(f"[MEMORY] summarizing conversation for {user_id}, thread {thread_id}")
    try:
        # fetch recent messages (bounded)
        msgs = await async_retry(lambda: list_thread_messages(thread_id, limit=MAX_HISTORY_MESSAGES))
        # safe parse: ensure structure exists
        data = getattr(msgs, "data", None) or []
        # build history with safe checks
        history_lines: List[str] = []
        for msg in reversed(data):
            role = getattr(msg, "role", "unknown")
            text_val = ""
            # defensive checks for content nested fields
            try:
                content = getattr(msg, "content", [])
                if content and isinstance(content, list):
                    # try to find text items
                    first = content[0]
                    text_val = getattr(first, "text", None)
                    if text_val:
                        # text_val may be an object with .value
                        text_val = getattr(text_val, "value", text_val)
            except Exception:
                text_val = ""
            history_lines.append(f"{role}: {text_val}")

        history = "\n".join(history_lines)
        if not history:
            logger.info("[MEMORY] no history to summarize")
            return

        prompt = f"Summarize concisely in a few bullet points for memory storage. Focus on user needs, preferences, contact info, products of interest, budget, and the conversation's last state.\n\nConversation:\n{history}\n\nSummary:"
        # call completion (blocking via thread)
        def call_completion():
            return client.chat.completions.create(model="gpt-4.1-mini", messages=[{"role":"system","content":prompt}])
        resp = await asyncio.to_thread(call_completion)
        # defensive extraction
        summary = ""
        try:
            summary = resp.choices[0].message.content.strip()
        except Exception:
            # fallback to any available text
            try:
                summary = getattr(resp.choices[0].message, "content", "")
            except Exception:
                summary = ""
        if summary:
            sessions_collection.update_one({"_id": user_id}, {"$set": {"conversation_summary": summary}})
            logger.info(f"[MEMORY] saved summary for {user_id}")
    except Exception as e:
        logger.exception(f"[MEMORY] failed to summarize for {user_id}: {e}")

# -------------------------
# Core: process user queue
# -------------------------
async def process_user_queue(user_id: str):
    """
    Single consumer for a user's queue. It batches messages arriving within BATCH_WAIT_TIME seconds.
    """
    logger.info(f"[PROCESSOR] started processor for {user_id}")
    queue: asyncio.Queue = user_queues[user_id]
    while True:
        try:
            # wait for first message (blocking)
            first = await queue.get()
            batch: Deque[str] = deque()
            batch.append(first)
            # now collect for BATCH_WAIT_TIME
            try:
                # collect more items until timeout
                while True:
                    item = await asyncio.wait_for(queue.get(), timeout=BATCH_WAIT_TIME)
                    batch.append(item)
            except asyncio.TimeoutError:
                pass  # time to process the batch

            texts = list(batch)
            logger.info(f"[PROCESSOR] user={user_id} batch_size={len(texts)}")
            MET_processed.inc()

            # fetch session (fresh)
            session = sessions_collection.find_one({"_id": user_id})
            if not session:
                logger.warning(f"[PROCESSOR] session disappeared for {user_id}, skipping")
                continue

            combined = "\n".join(texts)

            # ensure thread exists
            thread_id = session.get("openai_thread_id")
            if not thread_id:
                try:
                    thread_id = await async_retry(create_thread)
                    if thread_id:
                        sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
                        logger.info(f"[ASSISTANT] created thread {thread_id} for {user_id}")
                except Exception as e:
                    logger.exception(f"[ASSISTANT] failed to create thread for {user_id}: {e}")
                    MET_errors.inc()
                    continue

            # prepare enriched content with memory if exists
            summary = session.get("conversation_summary", "")
            enriched_content = combined
            if summary:
                enriched_content = f"For context: {summary}\nNow respond to the user's message(s): {combined}"

            # safe add message to thread (with retry)
            try:
                await async_retry(lambda: create_thread_message(thread_id=thread_id, role="user", content=enriched_content))
            except Exception as e:
                logger.exception(f"[ASSISTANT] failed to add message {e}")
                MET_errors.inc()
                continue

            # start run
            try:
                run = await async_retry(lambda: create_thread_run(thread_id=thread_id, assistant_id=ASSISTANT_ID_PREMIUM))
            except Exception as e:
                logger.exception(f"[ASSISTANT] failed to start run: {e}")
                MET_errors.inc()
                continue

            run_id = getattr(run, "id", None)
            status = getattr(run, "status", None)
            # poll with timeout/backoff
            start_ts = asyncio.get_event_loop().time()
            try:
                with MET_response_time.time():
                    while status in ("queued", "in_progress"):
                        if asyncio.get_event_loop().time() - start_ts > OPENAI_RUN_TIMEOUT:
                            logger.error(f"[ASSISTANT] run {run_id} timeout for {user_id}")
                            raise TimeoutError("OpenAI run timeout")
                        await asyncio.sleep(1)
                        run = await async_retry(lambda: retrieve_run(thread_id=thread_id, run_id=run_id))
                        status = getattr(run, "status", None)

                if status == "completed":
                    # fetch last message safely
                    msgs = await async_retry(lambda: list_thread_messages(thread_id=thread_id, limit=1))
                    data = getattr(msgs, "data", []) or []
                    reply = ""
                    if data:
                        msg = data[0]
                        # defensive extraction
                        try:
                            content = getattr(msg, "content", [])
                            if content and isinstance(content, list):
                                first = content[0]
                                text_obj = getattr(first, "text", None)
                                if text_obj:
                                    reply = getattr(text_obj, "value", str(text_obj))
                        except Exception:
                            reply = ""
                    if not reply:
                        logger.warning(f"[ASSISTANT] no reply content for {user_id}")
                        MET_errors.inc()
                        continue

                    # send via ManyChat with retry/backoff
                    try:
                        await async_retry(lambda: manychat_send(user_id, reply, platform=session.get("platform", "facebook")))
                    except Exception as e:
                        logger.exception(f"[SENDER] failed to send to ManyChat for {user_id}: {e}")
                        MET_errors.inc()
                        continue

                    # start background summarizer (fire-and-forget)
                    asyncio.create_task(summarize_and_save_conversation(user_id, thread_id))
                else:
                    logger.error(f"[ASSISTANT] run ended unexpectedly with status={status} for {user_id}")
                    MET_errors.inc()
            except Exception as e:
                logger.exception(f"[PROCESSOR] error while waiting for run: {e}")
                MET_errors.inc()
                continue
        except asyncio.CancelledError:
            logger.info(f"[PROCESSOR] processor cancelled for {user_id}")
            break
        except Exception as e:
            logger.exception(f"[PROCESSOR] unexpected error: {e}")
            MET_errors.inc()
            await asyncio.sleep(1)

# -------------------------
# Public helper to enqueue a message for a user (async)
# -------------------------
def enqueue_message(user_id: str, session: dict, text: str):
    """
    This runs in Flask sync context — simply put item into asyncio queue.
    Ensure a processor task exists for the user.
    """
    loop = asyncio.get_event_loop()
    # ensure queue and task exists
    if user_id not in user_processors or user_processors[user_id].done():
        q = asyncio.Queue()
        user_queues[user_id] = q
        task = loop.create_task(process_user_queue(user_id))
        user_processors[user_id] = task
        logger.info(f"[ENQUEUE] started processor task for {user_id}")
    else:
        q = user_queues[user_id]
    # put message into queue
    q.put_nowait(text)
    logger.info(f"[ENQUEUE] queued message for {user_id} (queue size approx: {q.qsize()})")

# -------------------------
# Flask webhook (sync) — uses enqueue_message
# -------------------------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook_handler():
    MET_requests.inc()
    auth_header = request.headers.get("Authorization")
    if not MANYCHAT_SECRET_KEY or auth_header != f"Bearer {MANYCHAT_SECRET_KEY}":
        logger.critical("Unauthorized webhook attempt")
        return jsonify({"status":"error","message":"Unauthorized"}), 403

    data = request.get_json(force=True, silent=True)
    if not data or not data.get("full_contact"):
        logger.error("Invalid webhook payload")
        return jsonify({"status":"error","message":"Invalid data"}), 400

    session = get_or_create_session_from_contact_sync(data["full_contact"])
    if not session:
        logger.error("Failed to create/get session")
        return jsonify({"status":"error","message":"Failed to create session"}), 500

    contact_data = data.get("full_contact", {})
    last_input = contact_data.get("last_text_input") or contact_data.get("last_input_text") or data.get("last_input")
    if not last_input:
        logger.info("No text input to process")
        return jsonify({"status":"no_input_received"})

    user_id = str(session["_id"])
    try:
        enqueue_message(user_id, session, last_input)
    except Exception as e:
        logger.exception("Failed to enqueue message")
        MET_errors.inc()
        return jsonify({"status":"error","message":"enqueue_failed"}), 500

    return jsonify({"status":"received"})

@app.route("/")
def home():
    return "✅ Async Bot running (v2) with per-user queues and backoff."

# -------------------------
# Graceful shutdown helper (optional)
# -------------------------
def shutdown():
    logger.info("Shutting down processors...")
    for uid, task in user_processors.items():
        if not task.done():
            task.cancel()

if __name__ == "__main__":
    # If running directly for local testing, start the Flask dev server (not recommended for prod)
    try:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
    finally:
        shutdown()
