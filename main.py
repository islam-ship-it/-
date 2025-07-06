import os
import time
import json
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient

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


# إدارة الجلسات
def get_session(user_id):
    session = sessions_collection.find_one({"_id": user_id})
    if not session:
        session = {"_id": user_id, "history": [], "thread_id": None, "message_count": 0, "name": ""}
    else:
        if "history" not in session:
            session["history"] = []
        if "thread_id" not in session:
            session["thread_id"] = None
        if "message_count" not in session:
            session["message_count"] = 0
        if "name" not in session:
            session["name"] = ""
    return session


def save_session(user_id, session_data):
    session_data["_id"] = user_id
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)


@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر شغال تمام!"


def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        return response.json()
    except Exception as e:
        print("❌ ZAPI Error:", e)
        return {"status": "error", "message": str(e)}


def organize_reply(text):
    url = f"{OPENROUTER_API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "mistral/mistral-7b-instruct",
        "messages": [
            {"role": "system", "content": "من فضلك نظملي الرسالة بشكل احترافي، استخدم رموز (✅ 🔹 💳)، خلي كل معلومة بسطر واضح."},
            {"role": "user", "content": text}
        ]
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("❌ Organizing Error:", e)
        return text


def ask_assistant(message, sender_id, name=""):
    session = get_session(sender_id)

    # تسجيل الاسم لو مش موجود
    if name and not session.get("name"):
        session["name"] = name

    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id

    # تحديث بيانات الجلسة
    session["message_count"] += 1
    session["history"].append({"role": "user", "content": message})
    save_session(sender_id, session)

    # تجهيز محتوى للمساعد مع بيانات إضافية
    intro = f"أنت تتعامل مع عميل اسمه: {session['name'] or 'غير معروف'}، رقمه: {sender_id}. هذه الرسالة رقم {session['message_count']} من العميل."
    full_message = f"{intro}\n\nالرسالة: {message}"

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
            return organize_reply(reply)
    return "⚠ حصلت مشكلة مؤقتة، حاول تاني."


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("\n📥 بيانات ZAPI:", data)

    sender = data.get("phone") or data.get("From")
    msg = data.get("text", {}).get("message") or data.get("body", "")
    msg_type = data.get("type", "")
    name = data.get("pushname", "")

    if not sender:
        return jsonify({"status": "no sender"}), 400

    if msg:
        reply = ask_assistant(msg, sender, name)
        send_message(sender, reply)
        return jsonify({"status": "sent"}), 200

    return jsonify({"status": "ignored"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
