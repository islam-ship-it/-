import os
import time
import json
import requests
import threading
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timedelta

# إعدادات البيئة
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
MONGO_URI = os.getenv("MONGO_URI")

client_db = MongoClient(MONGO_URI)
db = client_db["whatsapp_bot"]
sessions_collection = db["sessions"]

app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

pending_messages = {}
timers = {}

confirmation_keywords = [
    "تم تأكيد طلبك", "تم استلام التحويل", "تم تنفيذ الطلب", "✅ تم تنفيذ العملية",
]

# إدارة الجلسات
def get_session(user_id):
    session = sessions_collection.find_one({"_id": user_id})
    if not session:
        session = {"_id": user_id, "history": [], "thread_id": None, "message_count": 0, "name": "", "block_until": None}
    return session or {"_id": user_id, "history": [], "thread_id": None, "message_count": 0, "name": "", "block_until": None}

def save_session(user_id, session_data):
    session_data["_id"] = user_id
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)

def block_client_24h(user_id):
    session = get_session(user_id)
    session["block_until"] = (datetime.utcnow() + timedelta(hours=24)).isoformat()
    save_session(user_id, session)

@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر شغال تمام!"

def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        requests.post(url, headers=headers, json=payload)
    except Exception as e:
        print(f"❌ ZAPI Error: {e}")

def ask_assistant(message, sender_id, name=""):
    session = get_session(sender_id)
    if name and not session.get("name"):
        session["name"] = name

    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id

    session["message_count"] += 1
    session["history"].append({"role": "user", "content": message})
    session["history"] = session["history"][-10:]
    save_session(sender_id, session)

    intro = f"أنت تتعامل مع عميل اسمه: {session['name'] or 'غير معروف'}، رقمه: {sender_id}. هذه الرسالة رقم {session['message_count']} من العميل."
    full_message = f"{intro}\n\nالرسالة:\n{message}"

    print(f"📨 استلام رسالة من {sender_id} - الاسم: {session['name']}\n{message}")

    client.beta.threads.messages.create(thread_id=session["thread_id"], role="user", content=full_message)
    run = client.beta.threads.runs.create(thread_id=session["thread_id"], assistant_id=ASSISTANT_ID)

    while True:
        run_status = client.beta.threads.runs.retrieve(thread_id=session["thread_id"], run_id=run.id)
        if run_status.status == "completed":
            break
        time.sleep(2)

    messages = client.beta.threads.messages.list(thread_id=session["thread_id"])
    for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            reply = msg.content[0].text.value.strip()
            if any(kw in reply for kw in confirmation_keywords):
                block_client_24h(sender_id)
                reply += "\n✅ تم استقبال طلبك، نرجو الانتظار حتى انتهاء التنفيذ."
            return reply
    return "⚠ حصلت مشكلة مؤقتة، حاول تاني."

def ask_assistant_with_image(image_url, sender_id):
    session = get_session(sender_id)
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id
        save_session(sender_id, session)

    print(f"🖼 إرسال صورة للمساعد: {image_url}")

    client.beta.threads.messages.create(
        thread_id=session["thread_id"],
        role="user",
        content=[{"type": "image_url", "image_url": image_url}]
    )
    run = client.beta.threads.runs.create(thread_id=session["thread_id"], assistant_id=ASSISTANT_ID)

    while True:
        run_status = client.beta.threads.runs.retrieve(thread_id=session["thread_id"], run_id=run.id)
        if run_status.status == "completed":
            break
        time.sleep(2)

    messages = client.beta.threads.messages.list(thread_id=session["thread_id"])
    for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            reply = msg.content[0].text.value.strip()
            return reply
    return "⚠ حصلت مشكلة مؤقتة مع الصورة، حاول تاني."

def process_pending_messages(sender, name):
    time.sleep(8)
    combined = "\n".join(pending_messages[sender])
    reply = ask_assistant(combined, sender, name)
    send_message(sender, reply)
    pending_messages[sender] = []
    timers.pop(sender, None)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    sender = data.get("phone") or data.get("From")
    msg = data.get("text", {}).get("message") or data.get("body", "")
    msg_type = data.get("type", "")
    name = data.get("pushname") or data.get("senderName") or ""

    print(f"📥 بيانات الواتساب: {data}")

    if not sender:
        return jsonify({"status": "no sender"}), 400

    session = get_session(sender)
    if session.get("block_until"):
        block_time = datetime.fromisoformat(session["block_until"])
        if datetime.utcnow() < block_time:
            send_message(sender, "✅ تم استقبال طلبك، نرجو الانتظار حتى انتهاء التنفيذ.")
            return jsonify({"status": "blocked"}), 200

    if msg_type == "image":
        image_url = data.get("image", {}).get("url")
        if image_url:
            print(f"✅ تم استقبال صورة من العميل: {image_url}")
            reply = ask_assistant_with_image(image_url, sender)
            send_message(sender, reply)
        return jsonify({"status": "image received"}), 200

    if msg:
        if sender not in pending_messages:
            pending_messages[sender] = []
        pending_messages[sender].append(msg)

        if sender not in timers:
            timers[sender] = threading.Thread(target=process_pending_messages, args=(sender, name))
            timers[sender].start()

    return jsonify({"status": "received"}), 200

if _name_ == "__main__":
    app.run(host="0.0.0.0", port=5000)
