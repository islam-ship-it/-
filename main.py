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

# عميل النموذج
client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)

# الرد على الرسالة
def ask_chatgpt(message, sender_id):
    session = get_session(sender_id)

    if not any(msg["role"] == "system" for msg in session["history"]):
        session["history"].insert(0, {
            "role": "system",
            "content": "أنت مساعد ذكي ومتخصص في الرد على استفسارات عملاء متجر المتابعين باللهجة المصرية. عندك ملف معرفة مرتبط يحتوي على بيانات الأسعار والخدمات. لما العميل يسأل عن الأسعار أو الخدمات، ابحث فقط في ملف المعرفة وطلع المعلومة الحقيقية الموجودة فيه. لو الخدمة اللي العميل بيسأل عنها مش موجودة في ملف المعرفة، ماتخترعش ولا تفترض إنك بتقدمها، قول بكل وضوح إن الخدمة غير متاحة حاليًا. اتكلم بأسلوب ودود ومقنع، وساعد العميل إنه ياخد قرار بسرعة ويكمل الطلب." 
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

        # الانتظار حتى يكتمل الـ Run
        while True:
            run_status = client.beta.threads.runs.retrieve(
                thread_id=session["thread_id"],
                run_id=run.id
            )
            if run_status.status == "completed":
                break
            time.sleep(1)

        response = client.beta.threads.messages.list(
            thread_id=session["thread_id"]
        )

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

# Webhook
@app.route("/")
def home():
    return "✅ البوت شغال"

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "✅ Webhook جاهز", 200

    data = request.json
    msg = data.get("text", {}).get("message") or data.get("body", "")
    sender = data.get("phone") or data.get("From")

    if not sender:
        return jsonify({"status": "no sender"}), 400

    reply = ask_chatgpt(msg, sender)
    send_message(sender, reply)
    return jsonify({"status": "sent"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
