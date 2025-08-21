import os
import time
import json
import requests
import threading
import asyncio
import logging
import random
from flask import Flask, request, jsonify
from asgiref.wsgi import WsgiToAsgi
from openai import OpenAI
from pymongo import MongoClient, ReturnDocument
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# --- الإعدادات ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
MONGO_URI = os.getenv("MONGO_URI")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

# MongoDB
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["assistant_db"]
threads_collection = db["threads"]

# Flask
app = Flask(__name__)
asgi_app = WsgiToAsgi(app)


# --- دوال مساعدة ---
def to_e164_digits(s):
    """توحيد الرقم لصيغة أرقام فقط (بدون +)."""
    return "".join(ch for ch in str(s) if ch.isdigit())


def send_meta_whatsapp_message(phone, message):
    """إرسال رسالة واتساب عبر Meta API الرسمي"""
    url = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": str(phone),
        "type": "text",
        "text": {"body": message or ""},
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        if response.status_code >= 400:
            logger.error(f"❌ [Meta API] {response.status_code} Error: {response.text}")
        response.raise_for_status()
        logger.info(f"✅ [Meta API] تم إرسال الرسالة إلى {phone} بنجاح.")
        return True
    except requests.exceptions.RequestException as e:
        error_text = getattr(e, "response", None).text if getattr(e, "response", None) else str(e)
        logger.error(f"❌ [Meta API] فشل إرسال الرسالة إلى {phone}: {error_text}")
        return False


def get_or_create_thread(user_id):
    thread = threads_collection.find_one({"user_id": user_id})
    if thread:
        return thread["thread_id"]
    new_thread = client.beta.threads.create()
    threads_collection.insert_one({"user_id": user_id, "thread_id": new_thread.id})
    return new_thread.id


def ask_assistant(message, user_id, user_name=None):
    thread_id = get_or_create_thread(user_id)
    content_list = []

    if message:
        content_list.append({"type": "text", "text": message})

    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=content_list
    )

    run = client.beta.threads.runs.create_and_poll(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID_PREMIUM
    )

    if run.status == "failed":
        try:
            logger.error(f"❌ OpenAI Run Error: {run.last_error}")
        except Exception:
            pass
        return "معذرةً، حصل خطأ مؤقت. جرب تاني بعد لحظات 🙏"

    messages = list(client.beta.threads.messages.list(thread_id=thread_id, run_id=run.id))
    if messages:
        for msg in messages:
            if msg.role == "assistant":
                return msg.content[0].text.value.strip()

    return "⚠️ حصل خطأ ومفيش رد."


# --- Webhook ---
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    logger.info(f"📩 [Webhook] {json.dumps(data, indent=2, ensure_ascii=False)}")

    if "entry" in data:
        for entry in data["entry"]:
            if "changes" in entry:
                for change in entry["changes"]:
                    if "value" in change and "messages" in change["value"]:
                        for message in change["value"]["messages"]:
                            sender_id = message["from"]
                            sender_name = message.get("profile", {}).get("name", "عميل")
                            msg_text = message.get("text", {}).get("body")

                            if msg_text:
                                reply_text = ask_assistant(msg_text, sender_id, sender_name)

                                # Fallback لو فاضي
                                if not reply_text or not str(reply_text).strip():
                                    reply_text = "تمام ✅ استلمت رسالتك وهردّ عليك حالًا."

                                to_number = to_e164_digits(sender_id)
                                send_meta_whatsapp_message(to_number, reply_text)

    return jsonify({"status": "ok"})


# --- تشغيل ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
