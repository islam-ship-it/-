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

@app.route('/')
def home():
    return "✅ البوت شغال! استخدم /webhook لاستقبال الرسائل."

def send_message(phone, message):
    url = f"{ZAPI_API_URL}/token/{ZAPI_TOKEN}/send-text"
    payload = {
        "phone": phone,
        "message": message
    }
    response = requests.post(url, json=payload)
    print("✅ تم إرسال الرد إلى العميل.")
    return response.json()

def ask_chatgpt(message):
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": static_prompt},
            {"role": "user", "content": message}
        ],
        "max_tokens": 500
    }
    print("🤖 بيتم إرسال الرسالة لـ ChatGPT...")
    response = requests.post(f"{OPENAI_API_BASE}/chat/completions", headers=headers, json=payload)

    try:
        result = response.json()
        print("📥 رد ChatGPT الكامل:")
        print(result)
        return result["choices"][0]["message"]["content"]
    except Exception as e:
        print("❌ حصل خطأ في تحليل الرد:", e)
        return "⚠ حصلت مشكلة مؤقتة في الاتصال بـ ChatGPT. جرّب تبعت تاني."

@app.route('/webhook', methods=['POST'])
def webhook():
    print("✅ Webhook تم استدعاؤه")

    data = request.json
    print("📦 البيانات المستلمة:")
    print(data)

    incoming_msg = data.get("text", {}).get("message")
    sender = data.get("phone")

    if incoming_msg and sender:
        print(f"📩 رسالة من: {sender} - {incoming_msg}")
        reply = ask_chatgpt(incoming_msg)
        send_message(sender, reply)
    else:
        print("⚠ البيانات غير مكتملة أو غير متوقعة")

    return jsonify({"status": "received"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)