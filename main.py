import os
import time
import json
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime

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


def get_session(user_id):
    session = sessions_collection.find_one({"_id": user_id})
    if session:
        return session
    return {
        "_id": user_id,
        "history": [],
        "thread_id": None,
        "name": None,
        "message_count": 0,
        "first_message_date": None
    }


def save_session(user_id, session_data):
    session_data["_id"] = user_id
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)


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
            {"role": "system", "content": "نظملي الرد بشكل احترافي مع الرموز ✅ 🔹 💬."},
            {"role": "user", "content": text}
        ]
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("❌ Organizing Error:", e)
        return text


def download_image(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ZAPI_TOKEN}"}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get("url")
        else:
            print("❌ Image Download Error:", response.text)
    except Exception as e:
        print("❌ Exception during image download:", e)
    return None


def ask_assistant(message, sender_id, sender_name=None):
    session = get_session(sender_id)
    
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id

    if sender_name and not session.get("name"):
        session["name"] = sender_name

    session["message_count"] += 1
    if not session.get("first_message_date"):
        session["first_message_date"] = datetime.utcnow().isoformat()

    history = session.get("history", [])
    last_messages = history[-10:]

    context_history = "\n".join([
        f"{msg['role'].capitalize()}: {msg['content']}" for msg in last_messages
    ])

    context_message = (
        f"بيانات العميل:\n"
        f"- الاسم: {session.get('name') or 'غير معروف'}\n"
        f"- الرقم: {sender_id}\n"
        f"- عدد مرات التواصل السابقة: {session['message_count'] - 1}\n"
        f"- تاريخ أول تواصل: {session.get('first_message_date')}\n"
        f"\nسياق آخر المحادثة:\n{context_history}\n"
        f"\nالرسالة الجديدة:\n{message}"
    )

    session.setdefault("history", []).append({"role": "user", "content": message})
    save_session(sender_id, session)

    thread_id = session["thread_id"]
    client.beta.threads.messages.create(thread_id=thread_id, role="user", content=context_message)

    run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=ASSISTANT_ID)
    while True:
        if client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id).status == "completed":
            break
        time.sleep(2)

    for msg in sorted(client.beta.threads.messages.list(thread_id=thread_id).data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            reply = msg.content[0].text.value.strip()
            session["history"].append({"role": "assistant", "content": reply})
            save_session(sender_id, session)
            return organize_reply(reply)

    return "⚠ في مشكلة مؤقتة، حاول تاني."


def ask_assistant_with_image(image_url, sender_id, sender_name=None):
    session = get_session(sender_id)

    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id

    if sender_name and not session.get("name"):
        session["name"] = sender_name

    session["message_count"] += 1
    if not session.get("first_message_date"):
        session["first_message_date"] = datetime.utcnow().isoformat()

    history = session.get("history", [])
    last_messages = history[-10:]

    context_history = "\n".join([
        f"{msg['role'].capitalize()}: {msg['content']}" for msg in last_messages
    ])

    context_message = (
        f"بيانات العميل:\n"
        f"- الاسم: {session.get('name') or 'غير معروف'}\n"
        f"- الرقم: {sender_id}\n"
        f"- عدد مرات التواصل السابقة: {session['message_count'] - 1}\n"
        f"- تاريخ أول تواصل: {session.get('first_message_date')}\n"
        f"\nسياق آخر المحادثة:\n{context_history}\n"
        f"\nالعميل أرسل الصورة التالية: {image_url}"
    )

    session.setdefault("history", []).append({"role": "user", "content": f"[صورة] {image_url}"})
    save_session(sender_id, session)

    thread_id = session["thread_id"]
    client.beta.threads.messages.create(thread_id=thread_id, role="user", content=context_message)

    run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=ASSISTANT_ID)
    while True:
        if client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id).status == "completed":
            break
        time.sleep(2)

    for msg in sorted(client.beta.threads.messages.list(thread_id=thread_id).data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            reply = msg.content[0].text.value.strip()
            session["history"].append({"role": "assistant", "content": reply})
            save_session(sender_id, session)
            return organize_reply(reply)

    return "⚠ في مشكلة مؤقتة مع معالجة الصورة، حاول تاني."


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "✅ Webhook شغال", 200

    data = request.json
    sender = data.get("phone") or data.get("From")
    sender_name = data.get("pushname") or data.get("name") or None

    msg = data.get("text", {}).get("message") or data.get("body", "")
    msg_type = data.get("type")

    if msg_type == "image":
        media_id = data.get("image", {}).get("id")
        if media_id:
            image_url = download_image(media_id)
            if image_url:
                send_message(sender, ask_assistant_with_image(image_url, sender, sender_name))
                return jsonify({"status": "sent"}), 200

    if msg:
        send_message(sender, ask_assistant(msg, sender, sender_name))
        return jsonify({"status": "sent"}), 200

    return jsonify({"status": "ignored"}), 200


@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر شغال تمام!"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
