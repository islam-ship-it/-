import os
import time
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from session_storage import get_session, save_session

# إعدادات البيئة
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = "https://api.openai.com/v1"
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)

# دالة الرد من النموذج
def ask_chatgpt(message, sender_id):
    session = get_session(sender_id)

    if not any(msg["role"] == "system" for msg in session["history"]):
        session["history"].insert(0, {
            "role": "system",
            "content": "أنت مساعد ذكي بترد على عملاء متجر المتابعين باللهجة المصرية بطريقة ودودة."
        })

    session["history"].append({"role": "user", "content": message})

    try:
        if "thread_id" not in session or not session.get("thread_id"):
            thread = client.beta.threads.create()
            session["thread_id"] = thread.id

        client.beta.threads.messages.create(
            thread_id=session["thread_id"],
            role="user",
            content=message
        )

        run = client.beta.threads.runs.create(
            thread_id=session["thread_id"],
            assistant_id="asst_znBcj5OWBhyaXJ4nkXEJybtt"
        )

        while True:
            run_status = client.beta.threads.runs.retrieve(
                thread_id=session["thread_id"],
                run_id=run.id
            )
            if run_status.status == "completed":
                break
            time.sleep(1)

        response = client.beta.threads.messages.list(thread_id=session["thread_id"])

        for msg in reversed(response.data):
            if msg.role == "assistant":
                raw_reply = msg.content[0].text.value.strip()
                session["history"].append({"role": "assistant", "content": raw_reply})
                save_session(sender_id, session)
                return raw_reply

        return "⚠ في مشكلة تقنية مؤقتة. جرب تبعت تاني كمان شوية."

    except Exception as e:
        print("❌ GPT Error:", e)
        return "⚠ في مشكلة تقنية مؤقتة. جرب تبعت تاني كمان شوية."

# إرسال رسالة عبر ZAPI
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

# نقطة استقبال الرسائل
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    message = data.get("message")
    sender_id = data.get("sender_id")
    phone = data.get("phone")

    if not message or not sender_id or not phone:
        return jsonify({"error": "Missing data"}), 400

    reply = ask_chatgpt(message, sender_id)
    send_message(phone, reply)
    return jsonify({"status": "success", "reply": reply})

# تشغيل التطبيق
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
