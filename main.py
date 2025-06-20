import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = "https://openai.chatgpt4mena.com/v1"
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
ZAPI_API_URL = os.getenv("ZAPI_API_URL")

app = Flask(__name__)
session_memory = {}

def build_price_prompt():
    return "خدماتنا تشمل تزويد المتابعين والإعلانات الممولة والاشتراكات الشهرية 💼"

def ask_chatgpt(message, session=None):
    if session is None:
        session = []

    if not session:
        session.append({
            "role": "system",
            "content": "أنت مساعد ودود 🌟 ترد باللهجة المصرية لصفحة Followers Store بأفضل طريقة مفيدة ومقنعة."
        })

    session.append({"role": "user", "content": message})
    print("OPENAI_API_KEY being used:", OPENAI_API_KEY)

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "gpt-4",  # Changed من gpt-4.1 إلى gpt-4
        "messages": session,
        "max_tokens": 400
    }

    try:
        response = requests.post(f"{OPENAI_API_BASE}/chat/completions", headers=headers, json=payload)
        data = response.json()
        print("🔁 GPT raw response:", data)
        if "choices" in data and data["choices"]:
            reply = data["choices"][0]["message"]["content"].strip()
            session.append({"role": "assistant", "content": reply})
            return reply
        else:
            error_message = data.get("error", {}).get("message", "رد غير متوقع من OpenAI.")
            return f"⚠ حصلت مشكلة من السيرفر: {error_message}. جرب تاني بعد شوية."
    except Exception as e:
        print("❌ Exception:", e)
        return "⚠ في مشكلة تقنية حالياً. ابعتلي تاني بعد شوية"

def send_message(phone, message):
    url = f"{ZAPI_API_URL}/send-message?token={ZAPI_TOKEN}"
    payload = {
        "phone": phone,
        "message": message
    }
    response = requests.post(url, json=payload)
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

    incoming_msg = None
    sender = None

    if data and "text" in data and "message" in data["text"]:
        incoming_msg = data["text"]["message"]
    elif data and "body" in data:
        incoming_msg = data["body"]

    if data and "phone" in data:
        sender = data["phone"]
    elif data and "From" in data:
        sender = data["From"]

    if incoming_msg and sender:
        print(f"📩 رسالة من: {sender} - {incoming_msg}")
        if sender not in session_memory:
            session_memory[sender] = []
        reply = ask_chatgpt(incoming_msg, session_memory[sender])
        send_message(sender, reply)
    else:
        print(f"⚠ البيانات غير مكتملة أو غير متوقعة. Received data: {data}")

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
