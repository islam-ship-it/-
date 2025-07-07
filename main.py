import os
import time
import threading
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timedelta
from utils import extract_image_url_from_message

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

def get_session(user_id):
    session = sessions_collection.find_one({"_id": user_id})
    if not session:
        session = {"_id": user_id, "history": [], "thread_id": None, "message_count": 0, "name": "", "block_until": None}
    return session

def save_session(user_id, session_data):
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)

def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        requests.post(url, headers=headers, json=payload)
    except Exception as e:
        print(f"❌ Error sending message: {e}")

def process_pending_messages(sender, name):
    time.sleep(8)
    combined = "\n".join(pending_messages[sender])
    send_message(sender, f"📩 رسالتك:\n{combined}")
    pending_messages[sender] = []
    timers.pop(sender, None)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    sender = data.get("phone") or data.get("From")
    msg = data.get("text", {}).get("message") or data.get("body", "")
    msg_type = data.get("type", "")
    name = data.get("pushname") or data.get("senderName") or data.get("profileName") or ""

    print(f"\n📥 استقبال رسالة جديدة:\nالعميل: {sender} | الاسم: {name} | نوع الرسالة: {msg_type}")

    if not sender:
        return jsonify({"status": "no sender"}), 400

    session = get_session(sender)
    if session.get("block_until") and datetime.utcnow() < datetime.fromisoformat(session["block_until"]):
        print(f"🚫 العميل {sender} في فترة البلوك.")
        send_message(sender, "✅ طلبك تحت التنفيذ، نرجو الانتظار.")
        return jsonify({"status": "blocked"}), 200

    if msg_type == "image":
        print(f"🖼 استقبال بيانات الصورة كاملة:\n{data}")
        data["zapi_token"] = ZAPI_TOKEN
        image_url = extract_image_url_from_message(data)
        caption = data.get("image", {}).get("caption", "")

        if image_url:
            print(f"✅ رابط الصورة المستخرج: {image_url}")

            if not session.get("thread_id"):
                thread = client.beta.threads.create()
                session["thread_id"] = thread.id
                save_session(sender, session)

            msg_content = [
                {"type": "text", "text": "دي صورة من العميل"},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]

            if caption:
                msg_content.append({"type": "text", "text": f"تعليق العميل:\n{caption}"})

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
                    send_message(sender, reply)
                    return jsonify({"status": "image processed"}), 200

        else:
            print("⚠ لم يتمكن من استخراج رابط الصورة.")
            send_message(sender, "❌ في مشكلة في تحميل الصورة، جرب تبعتها تاني.")

        return jsonify({"status": "image failed"}), 200

    if msg:
        print(f"📩 رسالة نصية من العميل: {msg}")
        if sender not in pending_messages:
            pending_messages[sender] = []
        pending_messages[sender].append(msg)

        if sender not in timers:
            timers[sender] = threading.Thread(target=process_pending_messages, args=(sender, name))
            timers[sender].start()

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
