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

# إدارة الجلسات
def get_session(user_id):
    session = sessions_collection.find_one({"_id": user_id}) or {}
    session.setdefault("_id", user_id)
    session.setdefault("history", [])
    session.setdefault("thread_id", None)
    session.setdefault("message_count", 0)
    session.setdefault("name", "")
    session.setdefault("block_until", None)
    return session

def save_session(user_id, session_data):
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)
    print(f"💾 تم حفظ بيانات العميل {user_id}")

def block_client_24h(user_id):
    session = get_session(user_id)
    session["block_until"] = (datetime.utcnow() + timedelta(hours=24)).isoformat()
    save_session(user_id, session)
    print(f"🚫 تم قفل ردود البوت للعميل {user_id} لمدة 24 ساعة")

# إرسال رسالة واتساب
def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        print(f"📤 تم إرسال الرسالة للعميل {phone} | الحالة: {response.status_code}")
    except Exception as e:
        print(f"❌ خطأ في الإرسال: {e}")

# تنسيق الرد
def organize_reply(text):
    try:
        return text.replace("##BLOCK_CLIENT_24H##", "").strip()
    except:
        return text

# تحميل الصورة
def download_image(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ZAPI_TOKEN}"}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            link = response.json().get("url")
            print(f"✅ تم الحصول على رابط الصورة: {link}")
            return link
    except Exception as e:
        print(f"❌ خطأ في تحميل الصورة: {e}")
    return None

# إرسال للمساعد
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

    intro = f"عميل: {session['name'] or 'غير معروف'} | رقم: {sender_id}"
    full_message = f"{intro}\n{message}"

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
            print(f"💬 رد المساعد:\n{reply}")
            if "##BLOCK_CLIENT_24H##" in reply:
                block_client_24h(sender_id)
                return "✅ تم استقبال طلبك، نرجو الانتظار حتى انتهاء التنفيذ."
            return organize_reply(reply)
    return "⚠ حصلت مشكلة مؤقتة، حاول تاني."

# الرد على الصورة
def handle_image(sender, media_id, caption, name):
    image_url = download_image(media_id)
    if not image_url:
        return

    session = get_session(sender)
    if name and not session.get("name"):
        session["name"] = name
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id

    msg_content = [
        {"type": "text", "text": f"عميل: {session['name'] or 'غير معروف'} | رقم: {sender}"},
        {"type": "image_url", "image_url": image_url}
    ]
    if caption:
        msg_content.append({"type": "text", "text": f"تعليق:\n{caption}"})

    client.beta.threads.messages.create(thread_id=session["thread_id"], role="user", content=msg_content)
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
            print(f"💬 رد المساعد على الصورة:\n{reply}")
            if "##BLOCK_CLIENT_24H##" in reply:
                block_client_24h(sender)
                reply = "✅ تم استقبال طلبك، نرجو الانتظار حتى انتهاء التنفيذ."
            send_message(sender, organize_reply(reply))

# تجميع الرسائل
def process_pending_messages(sender, name):
    time.sleep(8)
    combined = "\n".join(pending_messages[sender])
    reply = ask_assistant(combined, sender, name)
    send_message(sender, reply)
    pending_messages[sender] = []
    timers.pop(sender, None)

# استقبال الويب هوك
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    sender = data.get("phone") or data.get("From")
    msg = data.get("text", {}).get("message") or data.get("body", "")
    msg_type = data.get("type", "")
    name = data.get("pushname") or data.get("senderName") or data.get("profileName") or ""

    if not sender:
        return jsonify({"status": "no sender"}), 400

    session = get_session(sender)
    block_until = session.get("block_until")
    if block_until and datetime.utcnow() < datetime.fromisoformat(block_until):
        send_message(sender, "✅ طلبك بالفعل تحت التنفيذ، نرجو الانتظار حتى انتهاء التنفيذ.")
        return jsonify({"status": "blocked"}), 200

    if msg_type == "image":
        media_id = data.get("image", {}).get("id")
        caption = data.get("image", {}).get("caption", "")
        if media_id:
            handle_image(sender, media_id, caption, name)
        return jsonify({"status": "sent"}), 200

    if msg:
        if sender not in pending_messages:
            pending_messages[sender] = []
        pending_messages[sender].append(msg)
        if sender not in timers:
            timers[sender] = threading.Thread(target=process_pending_messages, args=(sender, name))
            timers[sender].start()

    return jsonify({"status": "received"}), 200

# تشغيل السيرفر
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
