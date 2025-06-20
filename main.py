import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from static_replies import static_prompt, replies
from services_data import services

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = "https://openai.chatgpt4mena.com/v1"
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
ZAPI_API_URL = os.getenv("ZAPI_API_URL")

app = Flask(__name__)
session_memory = {}

def build_price_prompt():
    lines = []
    for item in services:
        line = f"- {item['count']} {item['type']} على {item['platform']}"
        if item['audience']:
            line += f" ({item['audience']})"
        line += f" = {item['price']} جنيه"
        if item['note']:
            line += f" ✅ {item['note']}"
        lines.append(line)
    return "\n".join(lines)

def ask_chatgpt(message, session=None):
    if session is None:
        session = []

    if not session:
        session.append({
            "role": "system",
            "content": static_prompt.format(
                prices=build_price_prompt(),
                confirm_text=replies["تأكيد_الطلب"]
            )
        })

    session.append({"role": "user", "content": message})

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "gpt-4.1",
        "messages": session,
        "max_tokens": 400
    }

    try:
        response = requests.post(f"{OPENAI_API_BASE}/chat/completions", headers=headers, json=payload)
        data = response.json()
        print("🔁 GPT raw response:", data)
        if "choices" in data:
            reply = data["choices"][0]["message"]["content"].strip()
            session.append({"role": "assistant", "content": reply})
            return reply
        else:
            return "⚠ حصلت مشكلة من السيرفر. جرب تاني بعد شوية."
    except Exception as e:
        print("❌ Exception:", e)
        return "⚠ في مشكلة تقنية حالياً. ابعتلي تاني بعد شوية."

def send_message(phone, message):
    url = f"{ZAPI_API_URL}/token/{ZAPI_TOKEN}/send-text"
    payload = {
        "phone": phone,
        "message": message
    }
    response = requests.post(url, json=payload)

    # ✅ طباعة رد ZAPI بالتفصيل
    try:
        print("📤 ZAPI response:", response.json())
    except Exception as e:
        print("⚠ خطأ في قراءة رد ZAPI:", e)
        print("📤 النص الكامل للرد:", response.text)

    print("✅ تم إرسال الرد إلى العميل.")
    return response.json()

@app.route("/")
def home():
    return "✅ البوت شغال"

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "✅ Webhook جاهز", 200

    print("✅ Webhook تم استدعاؤه")
    data = request.json
    print("📦 البيانات المستلمة:")
    print(data)

    incoming_msg = data.get("text", {}).get("message")
    sender = data.get("phone")

    if incoming_msg and sender:
        print(f"📩 رسالة من: {sender} - {incoming_msg}")
        if sender not in session_memory:
            session_memory[sender] = []
        reply = ask_chatgpt(incoming_msg, session_memory[sender])
        send_message(sender, reply)
    else:
        print("⚠ البيانات غير مكتملة أو غير متوقعة")

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
