import os
import time
import json
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime

# إعداد البيئة
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


# تخزين الجلسة
def get_session(user_id):
    session = sessions_collection.find_one({"_id": user_id})
    if session:
        return session
    return {
        "_id": user_id,
        "history": [],
        "thread_id": None,
        "name": None,
        "first_message_date": None,
        "last_message_date": None,
        "message_count": 0,
        "image_count": 0,
        "source_platform": "whatsapp",
        "tags": [],
        "status": "مفتوح"
    }


def save_session(user_id, session_data):
    session_data["_id"] = user_id
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)


# إرسال رسالة واتساب
def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}

    try:
        response = requests.post(url, headers=headers, json=payload)
        return response.json()
    except Exception as e:
        print("❌ خطأ في إرسال الرسالة:", e)
        return {"status": "error", "message": str(e)}


# تنسيق الرد
def organize_reply(text):
    url = f"{OPENROUTER_API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "mistral/mistral-7b-instruct",
        "messages": [
            {"role": "system", "content": "من فضلك، نظم الرد بشكل واضح وبسيط، استخدم رموز زي ✅ 🔹 💳."},
            {"role": "user", "content": text}
        ]
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("❌ خطأ تنظيم الرد:", e)
        return text


# تحميل الصورة من واتساب
def download_image(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ZAPI_TOKEN}"}

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get("url")
        else:
            print("❌ خطأ تحميل الصورة:", response.text)
            return None
    except Exception as e:
        print("❌ استثناء أثناء تحميل الصورة:", e)
        return None


# التفاعل مع نصوص العميل
def ask_assistant(message, sender_id):
    session = get_session(sender_id)
    client_name = session.get("name")
    now = datetime.utcnow().isoformat()

    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id
        session["first_message_date"] = now
        session["message_count"] = 0
        session["image_count"] = 0

    session["last_message_date"] = now
    session["message_count"] += 1

    session.setdefault("history", []).append({"role": "user", "content": message})
    save_session(sender_id, session)

    thread_id = session["thread_id"]

    client.beta.threads.messages.create(thread_id=thread_id, role="user", content=message)
    run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=ASSISTANT_ID)

    while True:
        run_status = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run_status.status == "completed":
            break
        time.sleep(2)

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            reply = organize_reply(msg.content[0].text.value.strip())
            if client_name:
                reply = f"أهلاً {client_name} 👋\n" + reply

            session["history"].append({"role": "assistant", "content": reply})
            save_session(sender_id, session)
            return reply

    return "⚠ حدثت مشكلة مؤقتة، حاول مرة أخرى."


# التفاعل مع الصور
def ask_assistant_with_image(image_url, sender_id):
    session = get_session(sender_id)
    client_name = session.get("name")
    now = datetime.utcnow().isoformat()

    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id
        session["first_message_date"] = now
        session["message_count"] = 0
        session["image_count"] = 0

    session["last_message_date"] = now
    session["message_count"] += 1
    session["image_count"] += 1

    session.setdefault("history", []).append({"role": "user", "content": f"[صورة مرسلة] {image_url}"})
    save_session(sender_id, session)

    thread_id = session["thread_id"]

    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=[{"type": "image_url", "image_url": image_url}]
    )

    run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=ASSISTANT_ID)

    while True:
        run_status = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run_status.status == "completed":
            break
        time.sleep(2)

    messages = client.beta.threads.messages.list(thread_id=thread_id)
    for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            reply = organize_reply(msg.content[0].text.value.strip())
            if client_name:
                reply = f"أهلاً {client_name} 👋\n" + reply

            session["history"].append({"role": "assistant", "content": reply})
            save_session(sender_id, session)
            return reply

    return "⚠ حدثت مشكلة مؤقتة في معالجة الصورة، حاول مجددًا."


# نقطة البداية
@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر يعمل بنجاح!"


# استقبال رسائل واتساب
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "✅ Webhook جاهز", 200

    data = request.json
    print("\n📦 البيانات المستلمة:", data)

    sender = data.get("phone") or data.get("From")
    name = data.get("pushname") or data.get("name")

    if not sender:
        return jsonify({"status": "لا يوجد مرسل"}), 400

    session = get_session(sender)
    if name:
        session["name"] = name
    save_session(sender, session)

    msg = data.get("text", {}).get("message") or data.get("body", "")
    msg_type = data.get("type")

    if msg_type == "image":
        media_id = data.get("image", {}).get("id")
        if media_id:
            image_url = download_image(media_id)
            if image_url:
                reply = ask_assistant_with_image(image_url, sender)
                send_message(sender, reply)
                return jsonify({"status": "تم الإرسال"}), 200

    if msg:
        reply = ask_assistant(msg, sender)
        send_message(sender, reply)
        return jsonify({"status": "تم الإرسال"}), 200

    return jsonify({"status": "تم التجاهل"}), 200


# تشغيل السيرفر
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
