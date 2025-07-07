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

# دوال الجلسات (نفس اللي عندك)

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
        print(f"🖼 استقبال صورة من العميل: {data}")
        data["zapi_token"] = ZAPI_TOKEN
        image_url = extract_image_url_from_message(data)
        caption = data.get("image", {}).get("caption", "")

        if image_url:
            print(f"✅ تم استخراج رابط الصورة: {image_url}")
            message_content = f"صورة من العميل: {image_url}"
            if caption:
                message_content += f"\nتعليق: {caption}"
            ask_assistant(message_content, sender, name)
            return jsonify({"status": "image processed"}), 200
        else:
            print("⚠ لم يتم استخراج رابط الصورة.")

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

