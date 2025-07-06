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


# كشف إعدادات البيئة واختبار الاتصال
def check_environment():
    print("\n=======================")
    print("✅ فحص الإعدادات والمتغيرات البيئية:")
    
    keys = [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ZAPI_BASE_URL",
        "ZAPI_INSTANCE_ID",
        "ZAPI_TOKEN",
        "CLIENT_TOKEN",
        "ASSISTANT_ID",
        "MONGO_URI"
    ]
    
    for key in keys:
        value = os.getenv(key)
        if value:
            print(f"✔ {key} = موجود ✅")
        else:
            print(f"❌ {key} = ناقص أو مش متسجل ❗")

    try:
        client_db.server_info()
        print("✅ تم الاتصال بقاعدة البيانات MongoDB بنجاح!")
    except Exception as e:
        print(f"❌ فشل الاتصال بـ MongoDB: {e}")

    print("=======================\n")


# تخزين الجلسات
def get_session(user_id):
    session = sessions_collection.find_one({"_id": user_id})
    if session:
        return session
    return {"_id": user_id, "history": [], "thread_id": None}


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
            {"role": "system", "content": "من فضلك، نظملي الرسالة دي بالشكل المثالي، خلي كل معلومة في سطر مستقل، استخدم الرموز بشكل احترافي زي (✅ - 🔹 - 💳)، متغيرش المعنى، بس نظم الشكل، العرض يكون واضح وسهل يتفهم."},
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


def download_image(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ZAPI_TOKEN}"}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data.get("url")
        else:
            print("❌ Image Download Error:", response.text)
            return None
    except Exception as e:
        print("❌ Exception during image download:", e)
        return None


def ask_assistant(message, sender_id):
    session = get_session(sender_id)
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id
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
    latest_reply = None
    for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            latest_reply = msg.content[0].text.value.strip()
            break

    if latest_reply:
        return organize_reply(latest_reply)
    return "⚠ في مشكلة مؤقتة، حاول تاني."


def ask_assistant_with_image(image_url, sender_id):
    session = get_session(sender_id)
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id
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
    latest_reply = None
    for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            latest_reply = msg.content[0].text.value.strip()
            break

    if latest_reply:
        return organize_reply(latest_reply)
    return "⚠ في مشكلة مؤقتة مع معالجة الصورة، حاول تاني."


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "✅ Webhook شغال", 200

    data = request.json
    print("\n=======================")
    print("📦 البيانات اللي جاية من ZAPI:")
    print(data)
    print("=======================\n")

    sender = data.get("phone") or data.get("From")
    if not sender:
        return jsonify({"status": "no sender"}), 400

    msg = data.get("text", {}).get("message") or data.get("body", "")
    msg_type = data.get("type")

    if msg_type == "image":
        print("🖼 صورة جات من العميل، بيانات الصورة:")
        print(data.get("image", {}))

        media_id = data.get("image", {}).get("id")
        if media_id:
            image_url = download_image(media_id)
            if image_url:
                reply = ask_assistant_with_image(image_url, sender)
                send_message(sender, reply)
                return jsonify({"status": "sent"}), 200

    if msg:
        reply = ask_assistant(msg, sender)
        send_message(sender, reply)
        return jsonify({"status": "sent"}), 200

    return jsonify({"status": "ignored"}), 200


if __name__ == "__main__":
    check_environment()
    app.run(host="0.0.0.0", port=5000)
