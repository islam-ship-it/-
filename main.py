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
    "تم تأكيد طلبك", "تم استلام التحويل", "تم تنفيذ الطلب",
    "✅ تم تنفيذ العملية", "✅ تم التفعيل", "تم تجهيز الطلب"
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
    print(f"💾 [جلسة محفوظة] العميل: {user_id} | الاسم: {session_data.get('name', 'غير معروف')}")

def block_client_24h(user_id):
    session = get_session(user_id)
    block_time = datetime.utcnow() + timedelta(hours=24)
    session["block_until"] = block_time.isoformat()
    save_session(user_id, session)
    print(f"⏸ [بلوك 24 ساعة] العميل: {user_id} حتى: {block_time}")

def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    print(f"📤 [إرسال رسالة] إلى: {phone} | المحتوى: {message[:100]}...")
    try:
        response = requests.post(url, headers=headers, json=payload)
        print(f"✅ [تم الإرسال] {response.text}")
    except Exception as e:
        print(f"❌ [خطأ في الإرسال] {e}")

def ask_assistant_with_image(image_url, sender_id):
    session = get_session(sender_id)
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id
        save_session(sender_id, session)

    print(f"🖼 [صورة] بدء إرسال صورة للمساعد: {image_url}")

    try:
        client.beta.threads.messages.create(
            thread_id=session["thread_id"],
            role="user",
            content=[{"type": "image_url", "image_url": image_url}]
        )
        print(f"✅ [صورة] الصورة اتبعتت للمساعد")
    except Exception as e:
        print(f"❌ [صورة] فشل إرسال الصورة: {e}")
        return "⚠ حصلت مشكلة أثناء إرسال الصورة"

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
            print(f"✅ [رد المساعد على الصورة]: {reply}")

            session["history"].append({"role": "assistant", "content": reply})
            save_session(sender_id, session)

            if any(kw in reply for kw in confirmation_keywords):
                block_client_24h(sender_id)
                reply += "\n✅ تم استقبال طلبك، نرجو الانتظار حتى انتهاء التنفيذ."

            return reply

    print(f"⚠ [صورة] المساعد مردش على الصورة")
    return "⚠ حصلت مشكلة مؤقتة مع الصورة"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    sender = data.get("phone") or data.get("From")
    msg = data.get("text", {}).get("message") or data.get("body", "")
    msg_type = data.get("type", "")
    name = data.get("pushname") or data.get("senderName") or ""

    print("\n====================")
    print(f"📥 بيانات الاستلام: {json.dumps(data, indent=2, ensure_ascii=False)}")
    print("====================\n")

    if not sender:
        print("❌ مفيش رقم عميل")
        return jsonify({"status": "no sender"}), 400

    session = get_session(sender)
    if session.get("block_until"):
        block_time = datetime.fromisoformat(session["block_until"])
        if datetime.utcnow() < block_time:
            print(f"⏸ العميل محظور لحد: {block_time}")
            send_message(sender, "✅ تم استقبال طلبك، نرجو الانتظار حتى انتهاء التنفيذ.")
            return jsonify({"status": "blocked"}), 200

    if msg_type == "image":
        image_data = data.get("image", {})
        print(f"🖼 [صورة] بيانات الصورة: {json.dumps(image_data, indent=2, ensure_ascii=False)}")
        image_url = image_data.get("url")

        if image_url:
            print(f"🔗 [رابط الصورة]: {image_url}")
            reply = ask_assistant_with_image(image_url, sender)
            send_message(sender, reply)
            return jsonify({"status": "image received"}), 200
        else:
            print("❌ [صورة] الرابط غير موجود")
            return jsonify({"status": "image error"}), 200

    if msg:
        if sender not in pending_messages:
            pending_messages[sender] = []
        pending_messages[sender].append(msg)

        if sender not in timers:
            timers[sender] = threading.Thread(target=process_pending_messages, args=(sender, name))
            timers[sender].start()

    return jsonify({"status": "received"}), 200

def process_pending_messages(sender, name):
    time.sleep(8)
    combined = "\n".join(pending_messages[sender])
    reply = ask_assistant_with_image(combined, sender)
    send_message(sender, reply)
    pending_messages[sender] = []
    timers.pop(sender, None)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
