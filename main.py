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

# دوال الجلسات
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
    print(f"💾 تم حفظ بيانات الجلسة للعميل {user_id}.")

# الحظر لمدة 24 ساعة
def block_client_24h(user_id):
    session = get_session(user_id)
    session["block_until"] = (datetime.utcnow() + timedelta(hours=24)).isoformat()
    save_session(user_id, session)
    print(f"🚫 العميل {user_id} تم حظره من الرد لمدة 24 ساعة.")

# إرسال رسالة واتساب
def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        print(f"📤 تم إرسال رسالة للعميل {phone}، الحالة: {response.status_code}")
    except Exception as e:
        print(f"❌ خطأ أثناء إرسال الرسالة: {e}")

# تحميل رابط الصورة من ZAPI
def download_image(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ZAPI_TOKEN}"}
    print(f"📥 محاولة تحميل الصورة من الرابط: {url}")
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            image_url = response.json().get("url")
            print(f"✅ رابط الصورة المستلم: {image_url}")
            return image_url
        else:
            print(f"❌ فشل تحميل الصورة، الكود: {response.status_code}, التفاصيل: {response.text}")
    except Exception as e:
        print(f"❌ خطأ أثناء تحميل الصورة: {e}")
    return None

# التفاعل مع المساعد
def ask_assistant(content, sender_id, name=""):
    session = get_session(sender_id)
    if name and not session.get("name"):
        session["name"] = name
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id

    session["message_count"] += 1
    session["history"].append({"role": "user", "content": str(content)})
    session["history"] = session["history"][-10:]
    save_session(sender_id, session)

    print("\n🚀 جاري إرسال للمساعد...")
    print(f"📨 محتوى الرسالة:\n{content}")

    client.beta.threads.messages.create(thread_id=session["thread_id"], role="user", content=content)
    run = client.beta.threads.runs.create(thread_id=session["thread_id"], assistant_id=ASSISTANT_ID)

    while True:
        run_status = client.beta.threads.runs.retrieve(thread_id=session["thread_id"], run_id=run.id)
        if run_status.status == "completed":
            break
        time.sleep(2)

    print("⏳ جاري جلب رد المساعد...")
    messages = client.beta.threads.messages.list(thread_id=session["thread_id"])
    for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            reply = msg.content[0].text.value.strip()
            print(f"✅ رد المساعد:\n{reply}")

            if "##BLOCK_CLIENT_24H##" in reply:
                block_client_24h(sender_id)
                return "✅ تم استقبال طلبك، نرجو الانتظار حتى انتهاء التنفيذ."

            return reply

    print("⚠ لم يتم استلام رد من المساعد.")
    return "⚠ مشكلة مؤقتة، حاول مرة أخرى."

# تجميع رسائل العميل
def process_pending_messages(sender, name):
    print(f"⏳ تجميع رسائل العميل {sender} لمدة 8 ثواني.")
    time.sleep(8)
    combined = "\n".join(pending_messages[sender])
    print(f"✅ الرسائل المجمعة من العميل:\n{combined}")

    reply = ask_assistant(combined, sender, name)
    send_message(sender, reply)
    pending_messages[sender] = []
    timers.pop(sender, None)
    print(f"🎯 الرد تم على جميع رسائل {sender}.")

# استقبال بيانات webhook
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print(f"\n📥 استقبال داتا كاملة:\n{json.dumps(data, indent=2, ensure_ascii=False)}")

    sender = data.get("phone") or data.get("From")
    msg = data.get("text", {}).get("message") or data.get("body", "")
    msg_type = data.get("type", "")
    name = data.get("pushname") or data.get("senderName") or data.get("profileName") or ""

    print(f"\n📊 تفاصيل:\nرقم العميل: {sender}\nنوع الرسالة: {msg_type}\nمحتوى الرسالة: {msg}")

    if not sender:
        print("❌ رقم العميل غير موجود.")
        return jsonify({"status": "no sender"}), 400

    session = get_session(sender)
    if session.get("block_until") and datetime.utcnow() < datetime.fromisoformat(session["block_until"]):
        send_message(sender, "✅ طلبك تحت التنفيذ، نرجو الانتظار.")
        return jsonify({"status": "blocked"}), 200

    if msg_type == "image":
        media_id = data.get("image", {}).get("id")
        caption = data.get("image", {}).get("caption", "")
        print(f"🖼 استقبال صورة | media_id: {media_id} | تعليق: {caption}")

        image_url = download_image(media_id) if media_id else None
        print(f"🌐 رابط الصورة: {image_url}")

        if image_url:
            msg_content = f"📷 صورة من العميل: {image_url}\nتعليق: {caption}" if caption else f"📷 صورة من العميل: {image_url}"
            reply = ask_assistant(msg_content, sender, name)
            print(f"📤 إرسال رد المساعد:\n{reply}")
            send_message(sender, reply)
            return jsonify({"status": "image processed"}), 200
        else:
            print("⚠ لم يتمكن من تحميل أو استخراج رابط الصورة.")

    if msg:
        print(f"💬 استقبال رسالة نصية من العميل: {msg}")
        if sender not in pending_messages:
            pending_messages[sender] = []
        pending_messages[sender].append(msg)

        if sender not in timers:
            print(f"⏳ بدء تجميع رسائل للعميل {sender} لمدة 8 ثواني.")
            timers[sender] = threading.Thread(target=process_pending_messages, args=(sender, name))
            timers[sender].start()

    return jsonify({"status": "received"}), 200

# الصفحة الرئيسية
@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر شغال تمام!"

# تشغيل التطبيق
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀 السيرفر شغال على البورت: {port}\n")
    app.run(host="0.0.0.0", port=port)
