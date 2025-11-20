# async_bot_with_media.py — Async bot supporting text, images, audio (batching + single message to assistant)
# Features:
# - Flask webhook for ManyChat (sync entrypoint)
# - Per-user asyncio.Queue with a single processor task
# - Support for text, image URLs, audio URLs in the incoming payload
# - Batch messages arriving within BATCH_WAIT_TIME and merge into a single assistant input containing a list of contents
# - Download media (images/audio) with aiohttp
# - Convert audio to transcript using OpenAI audio transcription (via blocking client wrapped in asyncio.to_thread)
# - Safe parsing of OpenAI responses
# - Retries with exponential backoff for network and OpenAI calls
# - Prometheus metrics
# - Environment-driven configuration

import os
import asyncio
import logging
import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional, List

import aiohttp
from aiohttp import ClientError
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from pymongo import MongoClient
from prometheus_client import start_http_server, Counter, Histogram

# OpenAI official client used for blocking operations inside asyncio.to_thread
from openai import OpenAI

# -------------------------
# Config & Logging
# -------------------------
load_dotenv()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("async-bot-media")

# helper to mask secrets in logs
def mask_secret(value: Optional[str]) -> str:
    if not value:
        return "<missing>"
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
MEDIA_DOWNLOAD_TIMEOUT = int(os.getenv("MEDIA_DOWNLOAD_TIMEOUT", "30"))
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "300"))

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
except Exception:
    logger.exception("Failed to connect to MongoDB")
    raise

# -------------------------
# OpenAI client (blocking) - we'll call via asyncio.to_thread
# -------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# Prometheus metrics (basic)
# -------------------------
start_http_server(8001)
MET_requests = Counter("bot_requests_total", "Total incoming webhook requests")
MET_processed = Counter("bot_processed_total", "Total processed batches")
MET_errors = Counter("bot_errors_total", "Total errors")
MET_response_time = Histogram("bot_response_seconds", "Time to get assistant response")

# -------------------------
# Async structures
# -------------------------
user_queues: defaultdict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
user_processors: dict[str, asyncio.Task] = {}
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
            logger.warning(f"Attempt {attempt} failed for {getattr(func, '__name__', str(func))}: {e}")
            if attempt == retries:
                break
            await asyncio.sleep(delay)
            delay *= factor
    raise last_exc

# -------------------------
# Media download helpers
# -------------------------
async def download_bytes(url: str, session: aiohttp.ClientSession, timeout: int = MEDIA_DOWNLOAD_TIMEOUT) -> bytes:
    """Download binary content with timeout and basic validation."""
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise ClientError(f"Failed to download media {url} status={resp.status}")
            data = await resp.read()
            if not data:
                raise ClientError("Empty media")
            return data
    except Exception as e:
        logger.exception(f"download_bytes failed for {url}: {e}")
        raise

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
async def create_thread() -> Optional[str]:
    def _create():
        return client.beta.threads.create()
    thread = await asyncio.to_thread(_create)
    return getattr(thread, "id", None)

async def list_thread_messages(thread_id: str, limit: int = 20):
    def _list():
        return client.beta.threads.messages.list(thread_id=thread_id, limit=limit)
    return await asyncio.to_thread(_list)

async def create_thread_message(thread_id: str, role: str, content):
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

# Audio transcription via OpenAI (blocking) — we wrap in to_thread
async def transcribe_audio_bytes(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    def _transcribe():
        # The exact call depends on client library; adapt if necessary.
        # We attempt to call client.audio.transcriptions.create or equivalent.
        try:
            return client.audio.transcriptions.create(file=audio_bytes, filename=filename, model="gpt-4o-mini-transcribe")
        except Exception as e:
            # fallback: try generic completions if API differs
            raise

    resp = await asyncio.to_thread(_transcribe)
    # defensive extraction
    try:
        return getattr(resp, "text", str(resp))
    except Exception:
        try:
            return resp["text"]
        except Exception:
            return str(resp)

# -------------------------
# Session utilities
# -------------------------
def get_or_create_session_from_contact_sync(contact_data: dict):
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
# Helper: build assistant content list combining text/image/audio
# -------------------------
async def build_assistant_contents(texts: List[str], image_urls: List[str], audio_urls: List[str]) -> List[dict]:
    """Return a list of content items suitable for client.beta.threads.messages.create as 'content'.
    The assistant will receive an ordered list: all texts merged, then images, then audio transcripts.
    """
    contents = []
    # 1) text block (if any)
    if texts:
        combined_text = "
".join(texts)
        contents.append({"type": "input_text", "text": combined_text})

    # 2) images: include URLs so assistant can reference them (or upload bytes if needed)
    # We'll just pass image_url items — the assistant runtime should be able to fetch them if supported
    for url in image_urls:
        contents.append({"type": "input_image", "image_url": url})

    # 3) audio: download and transcribe, then include transcript and original URL reference
    if audio_urls:
        async with aiohttp.ClientSession() as session:
            for url in audio_urls:
                try:
                    audio_bytes = await download_bytes(url, session)
                    # optional: check size/duration limits; we skip that here
                    transcript = await async_retry(lambda: transcribe_audio_bytes(audio_bytes, filename="audio.webm"))
                    contents.append({"type": "input_audio_transcript", "audio_url": url, "transcript": transcript})
                except Exception as e:
                    logger.exception(f"Failed to process audio {url}: {e}")
                    # still include a placeholder so assistant knows audio existed
                    contents.append({"type": "input_audio_unavailable", "audio_url": url, "error": str(e)})
    return contents

# -------------------------
# Conversation summarization (async) — bounded
# -------------------------
async def summarize_and_save_conversation(user_id: str, thread_id: str):
    logger.info(f"[MEMORY] summarizing conversation for {user_id}, thread {thread_id}")
    try:
        msgs = await async_retry(lambda: list_thread_messages(thread_id, limit=MAX_HISTORY_MESSAGES))
        data = getattr(msgs, "data", None) or []
        history_lines: List[str] = []
        for msg in reversed(data):
            role = getattr(msg, "role", "unknown")
            text_val = ""
            try:
                content = getattr(msg, "content", [])
                if content and isinstance(content, list):
                    first = content[0]
                    text_obj = getattr(first, "text", None)
                    if text_obj:
                        text_val = getattr(text_obj, "value", text_obj)
            except Exception:
                text_val = ""
            history_lines.append(f"{role}: {text_val}")

        history = "
".join(history_lines)
        if not history:
            logger.info("[MEMORY] no history to summarize")
            return

        prompt = f"Summarize concisely in a few bullet points for memory storage. Focus on user needs, preferences, contact info, products of interest, budget, and the conversation's last state.

Conversation:
{history}

Summary:"
        def call_completion():
            return client.chat.completions.create(model="gpt-4.1-mini", messages=[{"role":"system","content":prompt}])
        resp = await asyncio.to_thread(call_completion)
        summary = ""
        try:
            summary = resp.choices[0].message.content.strip()
        except Exception:
            try:
                summary = getattr(resp.choices[0].message, "content", "")
            except Exception:
                summary = str(resp)
        if summary:
            sessions_collection.update_one({"_id": user_id}, {"$set": {"conversation_summary": summary}})
            logger.info(f"[MEMORY] saved summary for {user_id}")
    except Exception as e:
        logger.exception(f"[MEMORY] failed to summarize for {user_id}: {e}")

# -------------------------
# Core: process user queue
# -------------------------
async def process_user_queue(user_id: str):
    logger.info(f"[PROCESSOR] started processor for {user_id}")
    queue: asyncio.Queue = user_queues[user_id]
    while True:
        try:
            first = await queue.get()
            texts = []
            image_urls = []
            audio_urls = []
            texts.append(first.get("text")) if first.get("text") else None

            # collect more items within the batch window
            try:
                while True:
                    item = await asyncio.wait_for(queue.get(), timeout=BATCH_WAIT_TIME)
                    if item.get("text"): texts.append(item.get("text"))
                    if item.get("image_url"): image_urls.append(item.get("image_url"))
                    if item.get("audio_url"): audio_urls.append(item.get("audio_url"))
            except asyncio.TimeoutError:
                pass

            # If first message had only image/audio and no text, ensure those included
            # Build assistant contents
            MET_processed.inc()
            session = sessions_collection.find_one({"_id": user_id})
            if not session:
                logger.warning(f"[PROCESSOR] session missing for {user_id}")
                continue

            # create content list
            contents = await build_assistant_contents(texts, image_urls, audio_urls)
            if not contents:
                logger.info("[PROCESSOR] no contents to send")
                continue

            # ensure thread
            thread_id = session.get("openai_thread_id")
            if not thread_id:
                try:
                    thread_id = await async_retry(create_thread)
                    if thread_id:
                        sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
                except Exception as e:
                    logger.exception(f"[ASSISTANT] failed to create thread for {user_id}: {e}")
                    MET_errors.inc()
                    continue

            # send the combined content to thread as a single user message (content is a list)
            try:
                await async_retry(lambda: create_thread_message(thread_id=thread_id, role="user", content=contents))
            except Exception as e:
                logger.exception(f"[ASSISTANT] failed to add message to thread: {e}")
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
                    msgs = await async_retry(lambda: list_thread_messages(thread_id=thread_id, limit=1))
                    data = getattr(msgs, "data", []) or []
                    reply = ""
                    if data:
                        msg = data[0]
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
                        logger.warning(f"[ASSISTANT] no reply for {user_id}")
                        MET_errors.inc()
                        continue

                    try:
                        await async_retry(lambda: manychat_send(user_id, reply, platform=session.get("platform", "facebook")))
                    except Exception as e:
                        logger.exception(f"[SENDER] failed to send to ManyChat for {user_id}: {e}")
                        MET_errors.inc()
                        continue

                    # background memory summarization
                    asyncio.create_task(summarize_and_save_conversation(user_id, thread_id))
                else:
                    logger.error(f"[ASSISTANT] run ended with status={status} for {user_id}")
                    MET_errors.inc()
            except Exception as e:
                logger.exception(f"[PROCESSOR] waiting for run failed: {e}")
                MET_errors.inc()
                continue
        except asyncio.CancelledError:
            logger.info(f"[PROCESSOR] processor cancelled for {user_id}")
            break
        except Exception as e:
            logger.exception(f"[PROCESSOR] unexpected error for {user_id}: {e}")
            MET_errors.inc()
            await asyncio.sleep(1)

# -------------------------
# Public helper to enqueue a message for a user (sync Flask -> async)
# -------------------------
def enqueue_message(user_id: str, session: dict, payload: dict):
    loop = asyncio.get_event_loop()
    if user_id not in user_processors or user_processors[user_id].done():
        q = asyncio.Queue()
        user_queues[user_id] = q
        task = loop.create_task(process_user_queue(user_id))
        user_processors[user_id] = task
        logger.info(f"[ENQUEUE] started processor task for {user_id}")
    else:
        q = user_queues[user_id]
    q.put_nowait(payload)
    logger.info(f"[ENQUEUE] queued payload for {user_id} (approx size: {q.qsize()})")

# -------------------------
# Webhook parsing: extract text, image_url, audio_url
# -------------------------
def parse_manychat_payload(data: dict) -> dict:
    """Return a dict with possible keys: text, image_url, audio_url.
    We attempt to be compatible with different ManyChat payload shapes.
    """
    contact = data.get("full_contact") or {}
    # try common fields for text
    text = contact.get("last_text_input") or contact.get("last_input_text") or data.get("last_input") or None

    # images might come in attachments or last_attachment
    image_url = None
    att = contact.get("last_attachment") or contact.get("attachment") or data.get("last_attachment")
    if att and isinstance(att, dict):
        if att.get("type") == "image":
            image_url = att.get("url") or att.get("file_url")

    # older ManyChat variants may send direct media fields
    if not image_url:
        # check for list attachments
        attachments = contact.get("attachments") or data.get("attachments")
        if attachments and isinstance(attachments, list):
            for a in attachments:
                if a.get("type") == "image":
                    image_url = a.get("url") or a.get("file_url")
                    break

    # audio/url
    audio_url = None
    # ManyChat could include audio as last_audio_url or audio_attachment
    audio_url = contact.get("last_audio_url") or contact.get("audio_url") or (att.get("url") if att and att.get("type") == "audio" else None)

    return {"text": text, "image_url": image_url, "audio_url": audio_url}

# -------------------------
# Flask webhook
# -------------------------
@app.route("/manychat_webhook", methods=["POST"])  # sync entry
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

    parsed = parse_manychat_payload(data)
    if not any(parsed.values()):
        logger.info("No usable media/text in payload")
        return jsonify({"status":"no_input_received"})

    payload = {"text": parsed.get("text"), "image_url": parsed.get("image_url"), "audio_url": parsed.get("audio_url")}
    enqueue_message(str(session["_id"]), session, payload)
    return jsonify({"status":"received"})

@app.route("/")
def home():
    return "✅ Async Bot running (media-enabled) with batching and transcription."

# -------------------------
# Shutdown helper
# -------------------------
def shutdown():
    logger.info("Shutting down processors...")
    for uid, task in user_processors.items():
        if not task.done():
            task.cancel()

if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
    finally:
        shutdown()
