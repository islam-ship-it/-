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

def get_session(user_id):
    session = sessions_collection.find_one({"_id": user_id})
    if not session:
        session = {"_id": user_id, "history": [], "thread_id": None, "message_count": 0, "name": "", "block_until": None}
    else:
        session.setdefault("history", [])
        session.setdefault("thread_id", None)
        session.setdefault("message_count", 0)
        session.setdefault("name", "")
        session.setdefault("block_until", None)
    return session

def save_session(user_id, session_data):
    session_data["_id"] = user_id
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)
    print(f"💾 تم حفظ بيانات الجلسة للعميل {user_id}.", flush=True)

def block_client_24h(user_id):
    session = get_session(user_id)
    session["block_until"] = (datetime.utcnow() + timedelta(hours=24)).isoformat()
    save_session(user_id, session)
    print(f"🚫 العميل {user_id} تم حظره من الرد لمدة 24 ساعة.", flush=True)

def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        print(f"📤 تم إرسال رسالة للعميل {phone}، الحالة: {response.status_code}", flush=True)
    except Exception as e:
        print(f"❌ خطأ أثناء إرسال الرسالة: {e}", flush=True)

def ask_assistant(content, sender_id, name=""):
    session = get_session(sender_id)
    if name and not session.get("name"):
        session["name"] = name
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id

    session["message_count"] += 1
    session["history"].append({"role": "user", "content": content})
    session["history"] = session["history"][-10:]
    save_session(sender_id, session)

    print(f"\n🚀 الداتا داخلة للمساعد:\n{json.dumps(content, indent=2, ensure_ascii=False)}", flush=True)

    try:
        client.beta.threads.messages.create(
            thread_id=session["thread_id"],
            role="user",
            content=content
        )
        print(f"✅ تم إرسال الداتا للمساعد بنجاح.", flush=True)

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
                print(f"💬 رد المساعد:\n{reply}", flush=True)
                if "##BLOCK_CLIENT_24H##" in reply:
                    block_client_24h(sender_id)
                return reply

    except Exception as e:
        print(f"❌ حصل استثناء أثناء الإرسال للمساعد: {e}", flush=True)

    return "⚠ مشكلة مؤقتة، حاول تاني."

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print(f"\n📥 البيانات المستلمة كاملة:\n{json.dumps(data, indent=2, ensure_ascii=False)}", flush=True)

    sender = data.get("phone") or data.get("From")
    name = data.get("pushname") or data.get("senderName") or data.get("profileName") or ""
    msg_type = data.get("type", "")
    session = get_session(sender)

    if msg_type == "image":
        image_info = data.get("image", {})
        image_url = image_info.get("imageUrl")
        caption = image_info.get("caption", "")
        print(f"📷 استقبال صورة من العميل: {sender}", flush=True)
        print(f"🌐 رابط الصورة: {image_url}", flush=True)
        print(f"✏ الكابشن: {caption}", flush=True)

        if image_url:
            message_content = [
                {"type": "text", "text": f"دي صورة من العميل رقم: {sender} - الاسم: {name}"},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
            if caption:
                message_content.append({"type": "text", "text": f"تعليق داخل الصورة:\n{caption}"})

            ask_assistant(message_content, sender, name)
            return jsonify({"status": "image processed"}), 200

    msg = data.get("text", {}).get("message") or data.get("body", "")
    if msg:
        print(f"💬 استقبال نص من العميل: {msg}", flush=True)
        ask_assistant([{"type": "text", "text": msg}], sender, name)

    return jsonify({"status": "received"}), 200

@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر شغال تمام!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
