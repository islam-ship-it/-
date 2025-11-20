# main_app.py
# Flask webhook + queue + debounce + media filtering (ignore .mp4) + merge text/images/audio
# Designed per user's request: support TEXT, IMAGES, AUDIO (non-mp4). If an incoming media URL
# is an MP4 (or contains 'audioclip'), it is ignored and the user immediately receives
# the reply: "ابعت صوت مش فيديو."  (don't process mp4 at all)
#
# Requirements (based on your env): Flask, requests, openai (OpenAI client), pymongo (optional)
#
# How it works:
# - Incoming webhook -> quick validation -> apply MP4 filter (reject and reply immediately)
# - Otherwise, append item to per-user queue and (re)start a 0.5s debounce Timer
# - When Timer fires: collect queued items for that user, merge them into one "batch message"
#   (text concatenation, image urls list, audio urls list)
# - For audio urls: attempt transcription (placeholder calling OpenAI whisper via client)
# - Send single combined prompt to assistant (OpenAI) and send assistant reply to client
#
# Notes:
# - This file contains helper placeholders for sending replies to Facebook/ManyChat.
#   Replace send_facebook_message(...) with your real integration code.
# - transcribe_audio(...) uses OpenAI's Audio Transcription endpoint; you may need to
#   adapt the call depending on the OpenAI SDK version you have.
# - Logging added to help debug. Make sure to set environment variables for API keys.

import os
import time
import threading
import tempfile
import requests
import logging
from flask import Flask, request, jsonify
from urllib.parse import urlparse

# OpenAI client - using the modern client if available
try:
    from openai import OpenAI
    client = OpenAI()
except Exception:
    client = None

# Configuration
DEBOUNCE_SECONDS = 0.5
MP4_REJECTION_MESSAGE = "ابعت صوت مش فيديو."  # reply when mp4 detected
ALLOWED_AUDIO_EXT = {'.m4a', '.mp3', '.wav', '.ogg', '.aac'}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Per-user queues and timers
user_queues = {}        # user_id -> list of items
user_timers = {}        # user_id -> threading.Timer
queues_lock = threading.Lock()

# ------------------------- Helper utilities -------------------------

def is_mp4_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    if '.mp4' in u:
        return True
    if 'audioclip' in u:
        # many Facebook audio clips are sent as mp4 container with 'audioclip' in name
        return True
    return False


def get_extension_from_url(url: str) -> str:
    try:
        path = urlparse(url).path
        _, dot, ext = path.rpartition('.')
        if dot:
            return '.' + ext.lower()
    except Exception:
        pass
    return ''


def send_facebook_message(user_id: str, text: str):
    """Placeholder: replace with actual ManyChat / Facebook send code.
    This function should send `text` to the user on the originating platform.
    Keep it synchronous or adapt to your async sending infra.
    """
    logging.info(f"[SENDER] to {user_id}: {text}")
    # TODO: implement HTTP POST to ManyChat/Facebook API here using stored credentials


def download_file(url: str, dest_path: str):
    logging.info(f"Downloading file: {url}")
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, stream=True, timeout=30)
    r.raise_for_status()
    with open(dest_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    logging.info(f"Saved to: {dest_path}")


def transcribe_audio(url: str) -> str:
    """Download audio file and transcribe using OpenAI's transcription (whisper) if client exists.
    Returns the transcription text or empty string on failure.
    NOTE: adapt this to your environment / SDK version.
    """
    if client is None:
        logging.warning("OpenAI client not available: skipping transcription")
        return ''
    try:
        with tempfile.NamedTemporaryFile(suffix=get_extension_from_url(url) or '.audio', delete=False) as tf:
            tmp_path = tf.name
        download_file(url, tmp_path)

        # Depending on SDK: use client.audio.transcriptions.create or use requests to OpenAI endpoint.
        # We'll attempt the client approach, but this may need adjustment to match your installed SDK.
        try:
            # Example for new OpenAI Python client (may vary):
            # response = client.audio.transcriptions.create(file=open(tmp_path, 'rb'), model='gpt-4o-transcribe')
            # Many setups use 'whisper-1' model name for transcription.
            with open(tmp_path, 'rb') as fh:
                resp = client.audio.transcriptions.create(file=fh, model='whisper-1')
            text = resp.get('text') if isinstance(resp, dict) else getattr(resp, 'text', '')
            logging.info(f"Transcription success: {text[:120]}")
            return text or ''
        except Exception as e:
            logging.exception("Transcription via client failed, attempting fallback or returning empty")
            return ''
    except Exception as e:
        logging.exception("Error downloading/transcribing audio")
        return ''
    finally:
        try:
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


# ------------------------- Queueing + Debounce logic -------------------------

def start_or_restart_timer(user_id: str):
    def timer_cb():
        try:
            process_user_queue(user_id)
        except Exception:
            logging.exception("Error in process_user_queue")

    with queues_lock:
        if user_id in user_timers and user_timers[user_id] is not None:
            user_timers[user_id].cancel()
        t = threading.Timer(DEBOUNCE_SECONDS, timer_cb)
        user_timers[user_id] = t
        t.start()


def enqueue_item(user_id: str, item: dict):
    """item: dict with keys: type in ('text','image','audio'), content: text or url"""
    with queues_lock:
        user_queues.setdefault(user_id, []).append(item)
    start_or_restart_timer(user_id)


def process_user_queue(user_id: str):
    logging.info(f"Processing queue for user {user_id}")
    with queues_lock:
        items = user_queues.pop(user_id, [])
        timer = user_timers.pop(user_id, None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    if not items:
        logging.info("No items to process")
        return

    # Merge items
    texts = []
    image_urls = []
    audio_urls = []

    for it in items:
        t = it.get('type')
        c = it.get('content')
        if t == 'text' and c:
            texts.append(c)
        elif t == 'image' and c:
            image_urls.append(c)
        elif t == 'audio' and c:
            audio_urls.append(c)

    # Transcribe audios (best-effort, sequential)
    transcriptions = []
    for aurl in audio_urls:
        try:
            txt = transcribe_audio(aurl)
            if txt:
                transcriptions.append(txt)
        except Exception:
            logging.exception("Failed to transcribe audio url: %s", aurl)

    # Build final assistant prompt
    prompt_parts = []
    if texts:
        prompt_parts.append("\n\n-- User texts: --\n" + "\n".join(texts))
    if transcriptions:
        prompt_parts.append("\n\n-- Audio transcriptions: --\n" + "\n".join(transcriptions))
    if image_urls:
        prompt_parts.append("\n\n-- Image URLs (for reference): --\n" + "\n".join(image_urls))

    final_prompt = "\n\n".join(prompt_parts).strip()
    if not final_prompt:
        # Nothing meaningful to send
        logging.info("No meaningful content after merge; skipping assistant call")
        return

    # Call assistant (placeholder) - adapt to your OpenAI usage (chat/completions, Threads, etc.)
    assistant_reply = call_assistant(final_prompt)

    # Send assistant reply back to the user (via Facebook / ManyChat)
    if assistant_reply:
        send_facebook_message(user_id, assistant_reply)


def call_assistant(prompt_text: str) -> str:
    """Placeholder for calling OpenAI assistant. Replace according to your preferred endpoint.
    Returns assistant's text reply or empty string.
    """
    logging.info("Calling assistant with prompt length=%d", len(prompt_text))
    if client is None:
        logging.warning("OpenAI client not available; returning placeholder reply")
        return "معليش، مش قادر أرد دلوقتي — فيه مشكلة في خدمة المعالج." 

    try:
        # Example: using chat completions (adjust model and payload per your setup)
        resp = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {"role": "system", "content": "أنت مساعد خبير. اقراء محتوى المستخدم وأجب باختصار وبلغة عربية فصحى أو عربية عامية حسب النص."},
                {"role": "user", "content": prompt_text}
            ],
            max_tokens=800,
        )
        # Parse response - adapt to SDK response structure
        if isinstance(resp, dict):
            # new clients often return dict
            choices = resp.get('choices', [])
            if choices:
                return choices[0].get('message', {}).get('content', '').strip()
        else:
            # object-like response
            try:
                return resp.choices[0].message.content.strip()
            except Exception:
                pass
    except Exception:
        logging.exception("Assistant call failed")
    return ''


# ------------------------- Webhook endpoint -------------------------

@app.route('/manychat_webhook', methods=['POST'])
def manychat_webhook():
    payload = request.get_json(silent=True) or {}
    logging.info(f"Webhook received: keys={list(payload.keys())}")

    # Extract platform user id and message info - adapt this section to ManyChat payload
    # For example payload: {"user_id": "123", "message": {"type":"text","text":"hello"}}

    user_id = str(payload.get('user_id') or payload.get('sender', {}).get('id') or payload.get('from'))
    message = payload.get('message') or payload.get('data') or payload

    # Basic guard
    if not user_id:
        logging.warning("Webhook missing user identification")
        return jsonify({'status': 'missing user id'}), 400

    # Normalize incoming items: check text, images, audio urls
    # This section must be adapted to your provider's exact JSON shape.
    # We'll attempt several common fields.
    items_to_add = []

    # Text
    text = None
    if isinstance(message, dict):
        text = message.get('text') or message.get('message') or message.get('body')
    if not text and isinstance(payload.get('text'), str):
        text = payload.get('text')

    if text:
        items_to_add.append({'type': 'text', 'content': text})

    # Images - try attachments / media
    media_urls = []
    # ManyChat/Facebook often uses 'attachments' array
    attachments = message.get('attachments') if isinstance(message, dict) else None
    if attachments and isinstance(attachments, list):
        for a in attachments:
            url = a.get('payload', {}).get('url') or a.get('url')
            if url:
                media_urls.append(url)

    # Also check direct fields that sometimes appear
    for key in ['image_url', 'image', 'photo']:
        v = message.get(key) if isinstance(message, dict) else None
        if isinstance(v, str):
            media_urls.append(v)

    # Audio fields
    audio_urls = []
    for key in ['audio_url', 'audio', 'voice']:
        v = message.get(key) if isinstance(message, dict) else None
        if isinstance(v, str):
            audio_urls.append(v)

    # Also check 'media' list or 'attachments' we already processed
    if isinstance(message, dict) and message.get('media'):
        for m in message.get('media'):
            if isinstance(m, str):
                media_urls.append(m)
            elif isinstance(m, dict):
                url = m.get('url') or m.get('src')
                if url:
                    media_urls.append(url)

    # Merge discovered media into image/audio classification
    for url in media_urls:
        if not url:
            continue
        if is_mp4_url(url):
            # immediate rejection: mp4 found -> send short reply and DO NOT enqueue
            logging.info(f"MP4 detected for user {user_id}: {url} -> rejecting")
            send_facebook_message(user_id, MP4_REJECTION_MESSAGE)
            # Important: if mp4 appears, do not enqueue any of this webhook's contents
            return jsonify({'status': 'rejected mp4'}), 200
        ext = get_extension_from_url(url)
        if ext in ALLOWED_AUDIO_EXT:
            audio_urls.append(url)
        else:
            # treat as image by default
            image_urls.append(url)

    # Add image items
    for img in image_urls:
        items_to_add.append({'type': 'image', 'content': img})

    # Add audio items
    for au in audio_urls:
        # if mp4 passed somehow, defend again
        if is_mp4_url(au):
            logging.info(f"MP4 found in audio loop for user {user_id} -> rejecting")
            send_facebook_message(user_id, MP4_REJECTION_MESSAGE)
            return jsonify({'status': 'rejected mp4'}), 200
        items_to_add.append({'type': 'audio', 'content': au})

    # If nothing meaningful, reply back politely
    if not items_to_add:
        logging.info("No meaningful content to queue; sending default prompt reply")
        send_facebook_message(user_id, "ما استلمتش حاجة صالحة. ممكن تبعت رسالة نصية أو صورة أو تسجيل صوتي؟")
        return jsonify({'status': 'no content'}), 200

    # Enqueue items
    for it in items_to_add:
        enqueue_item(user_id, it)

    # Acknowledge webhook receipt quickly (we don't say "thanks for the file")
    # We will send the user the real reply after processing the batch.
    return jsonify({'status': 'queued'}), 200


# ------------------------- Run Flask -------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
