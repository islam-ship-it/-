# main.py
# نسخة v23 — معالج Webhook ManyChat مُنظّم، يدعم نصوص، صور، وصوت (روابط)،
# يدير حجم المحتوى قبل الإرسال إلى OpenAI Threads API لتجنُّب خطأ string_above_max_length
# ملاحظات:  - اضبط متغيرات البيئة أدناه
#            - هذا ملف مستقل يمكن استبداله مباشرة

import os
import io
import json
import time
import logging
import asyncio
import traceback
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify

# استخدام عميل OpenAI الرسمي
try:
    from openai import OpenAI
except Exception:
    # إذا لم يكن متاحًا بنفس الاسم، فحاول الاستيراد القديم كنسخة احتياطية
    import openai

    class OpenAI:
        def __init__(self, **kwargs):
            self.__client = openai

        def __getattr__(self, name):
            return getattr(self.__client, name)

# ---------- إعداد اللوج ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------- إعداد التطبيق والمتغيرات ----------
app = Flask(__name__)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_API_BASE = os.environ.get("OPENAI_API_BASE")  # إن كنت تستخدم بنية خاصة
THREADS_API_MAX_CONTENT = 256000  # حد تقريبى من الخطأ الذي ظهر

# اختيارات التهيئة
ASSISTANT_THREAD_ID_TEMPLATE = os.environ.get("ASSISTANT_THREAD_ID_TEMPLATE", "thread_{user_id}")
# مثال: "thread_{user_id}" — ستُستبدل {user_id} بمعرف المستخدم من ManyChat

# تهيئة العميل
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- أدوات مساعدة ----------

def safe_get(d: Dict[str, Any], *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def extract_text_from_full_contact(full_contact: Dict[str, Any]) -> str:
    """According to logs, ManyChat puts latest user text in full_contact['last_input'].
    Fall back to other fields if missing.
    """
    text = safe_get(full_contact, "last_input")
    if text:
        return str(text)

    # Fallbacks (kept short and safe)
    for candidate in [
        safe_get(full_contact, "messages", 0, "text"),
        safe_get(full_contact, "message", "text"),
        safe_get(full_contact, "last_message", "text"),
    ]:
        if candidate:
            return str(candidate)

    return ""


def extract_attachment(full_contact: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return attachment dict if present with fields: type, url, filename(optional)
    ManyChat may include attachments in different keys; check several.
    """
    # Most reliable try
    att = safe_get(full_contact, "last_attachment") or safe_get(full_contact, "attachment")
    if isinstance(att, dict) and att.get("url"):
        return att

    # Try nested messages
    messages = full_contact.get("messages") or []
    if messages and isinstance(messages, list):
        for m in messages:
            if isinstance(m, dict) and m.get("attachment"):
                a = m.get("attachment")
                if isinstance(a, dict) and a.get("url"):
                    return a
    return None


def build_enriched_content(user_id: str, user_text: str, attachment: Optional[Dict[str, Any]], history: List[str]) -> str:
    """Create a single string payload but ensure it's under THREADS_API_MAX_CONTENT.
    Strategy:
      1) Start with user_text + attachment metadata
      2) Append as much recent history as fits
      3) If still too long, truncate the oldest history or compress with simple summarization
    """
    parts: List[str] = []
    parts.append(f"[user_id:{user_id}]")
    if user_text:
        parts.append("[user_text]")
        parts.append(user_text)

    if attachment:
        parts.append("[attachment]")
        att_type = attachment.get("type") or attachment.get("mime_type") or guess_type_from_url(attachment.get("url"))
        parts.append(f"type={att_type}")
        parts.append(f"url={attachment.get('url')}")

    if history:
        parts.append("[recent_history]")
        # append from newest to oldest until length limit
        for h in reversed(history):
            parts.append(h)
            candidate = "\n".join(parts)
            if len(candidate) > THREADS_API_MAX_CONTENT:
                parts.pop()  # remove the last added history that broke the limit
                break

    payload = "\n".join(parts)

    # If payload still exceeds limit (rare), aggressively truncate user_text
    if len(payload) > THREADS_API_MAX_CONTENT:
        # keep first N chars of user_text
        keep = THREADS_API_MAX_CONTENT - 2000
        if keep < 100:
            keep = 100
        truncated_text = (user_text[:keep] + "... [truncated]") if user_text else ""
        # rebuild minimal payload
        minimal = f"[user_id:{user_id}]\n[user_text]\n{truncated_text}"
        if attachment:
            minimal += f"\n[attachment]\ntype={att_type}\nurl={attachment.get('url')}"
        payload = minimal[:THREADS_API_MAX_CONTENT]

    return payload


def guess_type_from_url(url: Optional[str]) -> str:
    if not url:
        return "unknown"
    path = urlparse(url).path.lower()
    if path.endswith(('.mp3', '.wav', '.m4a', '.ogg')):
        return 'audio'
    if path.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
        return 'image'
    return 'file'


async def post_to_threads(thread_id: str, role: str, content: str, max_retries: int = 3) -> Dict[str, Any]:
    """Post message to OpenAI Threads API (beta/messages). Uses asyncio.to_thread to keep compatibility.
    Returns API response dict.
    """
    for attempt in range(1, max_retries + 1):
        try:
            # Some SDKs use client.beta.threads.messages.create
            if hasattr(client, 'beta') and hasattr(client.beta, 'threads'):
                # blocking call wrapped into thread
                return await asyncio.to_thread(
                    lambda: client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role=role,
                        content=content,
                    )
                )
            else:
                # Fallback: use direct REST via requests (simple)
                api_url = (OPENAI_API_BASE or 'https://api.openai.com') + f"/v1/threads/{thread_id}/messages"
                headers = {
                    'Authorization': f'Bearer {OPENAI_API_KEY}',
                    'Content-Type': 'application/json',
                }
                resp = requests.post(api_url, headers=headers, json={
                    'role': role,
                    'content': content,
                }, timeout=30)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning(f"Attempt {attempt} failed posting to threads: {e}")
            if attempt == max_retries:
                raise
            await asyncio.sleep(0.5 * attempt)


# ---------- Webhook handler ----------
@app.route('/manychat_webhook', methods=['POST'])
def manychat_webhook():
    try:
        payload = request.get_json(force=True)
        logger.info(f"تم استلام Webhook مفاتيح الحمولة = {list(payload.keys()) if isinstance(payload, dict) else 'N/A'}")

        # ManyChat full_contact payload often nested under 'full_contact'
        full_contact = payload.get('full_contact') if isinstance(payload, dict) else None
        if not full_contact:
            logger.warning("لم يتم توفير معرف الموضوع في خطاف الويب أو full_contact مفقود")
            return jsonify({"ok": False, "reason": "missing_full_contact"}), 400

        user_id = safe_get(full_contact, 'uid') or safe_get(full_contact, 'id') or safe_get(full_contact, 'visitor_id') or 'unknown_user'
        user_text = extract_text_from_full_contact(full_contact)
        attachment = extract_attachment(full_contact)

        # Build a compact history: ManyChat might not send full history; but if thread id exists in payload use it.
        recent_history = []
        # if the webhook includes 'chat_history' or similar
        raw_history = safe_get(full_contact, 'history') or safe_get(full_contact, 'messages') or []
        if isinstance(raw_history, list):
            # keep textual parts only, limit to last 8 entries
            txts = []
            for m in raw_history[-8:]:
                if isinstance(m, dict):
                    t = m.get('text') or m.get('message') or ''
                else:
                    t = str(m)
                if t:
                    txts.append(t)
            recent_history = txts

        enriched = build_enriched_content(user_id=user_id, user_text=user_text, attachment=attachment, history=recent_history)

        # pick thread id (per-user thread name templating)
        thread_id = ASSISTANT_THREAD_ID_TEMPLATE.format(user_id=user_id)

        # Avoid sending enormous payloads; we have already tried to guard.
        if len(enriched) > THREADS_API_MAX_CONTENT:
            logger.warning(f"Payload size {len(enriched)} exceeds limit, truncating further")
            enriched = enriched[:THREADS_API_MAX_CONTENT]

                # Ensure thread exists — create if 404
        try:
            # quick check: retrieve thread
            if hasattr(client, 'beta') and hasattr(client.beta, 'threads'):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(asyncio.to_thread(lambda: client.beta.threads.retrieve(thread_id=thread_id)))
                except Exception:
                    # create new thread
                    new_thread = loop.run_until_complete(asyncio.to_thread(lambda: client.beta.threads.create(id=thread_id)))
        except Exception as e:
            logger.warning(f"Thread check/create failed: {e}")

        # Post asynchronously to OpenAI threads
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        resp = loop.run_until_complete(post_to_threads(thread_id=thread_id, role='user', content=enriched))(post_to_threads(thread_id=thread_id, role='user', content=enriched))

        logger.info(f"تم إرسال المحتوى للمساعدة في thread {thread_id}")

        return jsonify({"ok": True, "thread_response": resp}), 200

    except Exception as e:
        logger.error("Exception in manychat_webhook: %s", traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------- Optional: health check & root ----------
@app.route('/', methods=['GET'])
def root():
    return jsonify({"ok": True, "msg": "service running"}), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Starting app on port {port}")
    app.run(host='0.0.0.0', port=port)
