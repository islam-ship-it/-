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
    # Note: For image messages, 'content' is already a list of dicts.
    # For text messages, 'content' is a list of dicts from process_pending_messages.
    # We need to ensure 'history' stores the actual content, not just the text.
    # This part might need careful handling if you want to store full multimodal history.
    # For now, let's assume 'content' is what we want to add to history.
    session["history"].append({"role": "user", "content": content})
    session["history"] = session["history"][-10:] # Keep last 10 entries
    save_session(sender_id, session)

    # --- START DIAGNOSTIC PRINTS ---
    print(f"\n🚀 الداتا داخلة للمساعد (داخل ask_assistant):\n{json.dumps(content, indent=2, ensure_ascii=False)}", flush=True)
    # --- END DIAGNOSTIC PRINTS ---

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
            # Add a small delay to avoid hammering the API
            time.sleep(1) # Changed from 2 to 1 for faster polling, adjust as needed

        messages = client.beta.threads.messages.list(thread_id=session["thread_id"])
        # Iterate through messages to find the latest assistant reply
        for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
            if msg.role == "assistant":
                # Check if content is a list and has text value
                if msg.content and hasattr(msg.content[0], 'text') and hasattr(msg.content[0].text, 'value'):
                    reply = msg.content[0].text.value.strip()
                    print(f"💬 رد المساعد:\n{reply}", flush=True)
                    if "##BLOCK_CLIENT_24H##" in reply:
                        block_client_24h(sender_id)
                    return reply
                else:
                    print(f"⚠️ رد المساعد لا يحتوي على نص متوقع: {msg.content}", flush=True)
                    return "⚠ مشكلة في استلام رد المساعد، حاول تاني."

    except Exception as e:
        print(f"❌ حصل استثناء أثناء الإرسال للمساعد أو استلام الرد: {e}", flush=True)

    return "⚠ مشكلة مؤقتة، حاول تاني."

def process_pending_messages(sender, name):
    print(f"⏳ تجميع رسائل العميل {sender} لمدة 8 ثواني.", flush=True)
    time.sleep(8)
    combined_text = "\n".join(pending_messages[sender])
    # For text messages, content needs to be a list of dicts for ask_assistant
    content = [{"type": "text", "text": combined_text}]
    
    # --- START DIAGNOSTIC PRINTS ---
    print(f"📦 محتوى الرسالة النصية المجمعة المرسل للمساعد:\n{json.dumps(content, indent=2, ensure_ascii=False)}", flush=True)
    # --- END DIAGNOSTIC PRINTS ---

    reply = ask_assistant(content, sender, name)
    send_message(sender, reply)
    pending_messages[sender] = []
    timers.pop(sender, None)
    print(f"🎯 الرد تم على جميع رسائل {sender}.", flush=True)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print(f"\n📥 البيانات المستلمة كاملة:\n{json.dumps(data, indent=2, ensure_ascii=False)}", flush=True)

    sender = data.get("phone") or data.get("From")
    msg = data.get("text", {}).get("message") or data.get("body", "")
    msg_type = data.get("type", "")
    name = data.get("pushname") or data.get("senderName") or data.get("profileName") or ""

    if not sender:
        print("❌ رقم العميل غير موجود.", flush=True)
        return jsonify({"status": "no sender"}), 400

    session = get_session(sender)
    if session.get("block_until") and datetime.utcnow() < datetime.fromisoformat(session["block_until"]):
        print(f"🚫 العميل {sender} في فترة الحظر.", flush=True)
        send_message(sender, "✅ طلبك تحت التنفيذ، نرجو الانتظار.")
        return jsonify({"status": "blocked"}), 200

    if msg_type == "image":
        image_url = data.get("image", {}).get("imageUrl")
        caption = data.get("image", {}).get("caption", "")
        
        # --- START DIAGNOSTIC PRINTS ---
        print(f"🌐 رابط الصورة المباشر المستلم من الـ webhook: {image_url}", flush=True)
        # --- END DIAGNOSTIC PRINTS ---

        if image_url:
            message_content = [
                {"type": "text", "text": f"دي صورة من العميل رقم: {sender} - الاسم: {name}"},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
            if caption:
                message_content.append({"type": "text", "text": f"تعليق داخل الصورة:\n{caption}"})

            # --- START DIAGNOSTIC PRINTS ---
            print(f"📦 محتوى رسالة الصورة المرسل للمساعد:\n{json.dumps(message_content, indent=2, ensure_ascii=False)}", flush=True)
            # --- END DIAGNOSTIC PRINTS ---

            ask_assistant(message_content, sender, name)
            return jsonify({"status": "image processed"}), 200
        else:
            print("⚠ لم يتم العثور على imageUrl داخل الرسالة الواردة من الـ webhook.", flush=True)
            # Optionally, send a message back to the user that image could not be processed
            # send_message(sender, "عذراً، لم أتمكن من معالجة الصورة. يرجى التأكد من أنها صورة صالحة.")
            return jsonify({"status": "no image url found"}), 200

    if msg:
        print(f"💬 استقبال رسالة نصية من العميل: {msg}", flush=True)
        if sender not in pending_messages:
            pending_messages[sender] = []
        pending_messages[sender].append(msg)

        if sender not in timers:
            timers[sender] = threading.Thread(target=process_pending_messages, args=(sender, name))
            timers[sender].start()

    return jsonify({"status": "received"}), 200

@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر شغال تمام!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
