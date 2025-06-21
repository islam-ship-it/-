from flask import Flask, request, jsonify
import requests
import os
from static_replies import static_prompt
from services_data import services

app = Flask(__name__)

# مفاتيح البيئة
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")

# ذاكرة الجلسة
session_memory = {}

# إرسال رسالة واتساب
def send_whatsapp_message(to_number, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    payload = {
        "to": to_number,
        "message": message
    }
    headers = {"Content-Type": "application/json"}
    print(f"[DEBUG] Sending to WhatsApp ➜ {payload}")
    response = requests.post(url, json=payload, headers=headers)
    print(f"[DEBUG] WhatsApp API Response: {response.status_code} - {response.text}")
    return response.json()

# استدعاء ChatGPT
def call_chatgpt(session_id, user_message):
    if session_id not in session_memory:
        session_memory[session_id] = []

    messages = session_memory[session_id]
    messages.append({"role": "user", "content": user_message})

    response = requests.post(
        "https://openai.chatgpt4mena.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "gpt-4o",
            "messages": [{"role": "system", "content": static_prompt(services)}] + messages,
            "temperature": 0.5
        }
    )

    print(f"[DEBUG] OpenAI Response Status: {response.status_code}")
    print(f"[DEBUG] OpenAI Raw Response: {response.text}")

    reply = response.json()["choices"][0]["message"]["content"]
    messages.append({"role": "assistant", "content": reply})
    return reply

# الراوت الأساسي
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "POST":
        data = request.get_json()
        try:
            msg = data["message"]
            phone = msg["from"]
            text = msg["text"]["body"]

            print(f"[RECEIVED] From: {phone} | Text: {text}")
            reply = call_chatgpt(phone, text)
            print(f"[REPLY] {reply}")
            send_whatsapp_message(phone, reply)

        except Exception as e:
            print("[ERROR]", str(e))
            return jsonify({"status": "error", "message": str(e)}), 500

        return jsonify({"status": "ok"}), 200

    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
