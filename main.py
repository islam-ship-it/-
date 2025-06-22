import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from openai import OpenAI

from static_replies import static_prompt, replies
from services_data import services

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = "https://openai.chatgpt4mena.com/v1"
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

app = Flask(__name__)
session_memory = {}

client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_API_BASE
)

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

def ask_chatgpt(message, sender_id):
    if sender_id not in session_memory:
        session_memory[sender_id] = [
            {
                "role": "system",
                "content": static_prompt.format(
                    prices=build_price_prompt(),
                    confirm_text=replies["تأكيد_الطلب"]
                )
            }
        ]

    session_memory[sender_id].append({"role": "user", "content": message})

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=session_memory[sender_id],
            max_tokens=500
        )
        reply_text = response.choices[0].message.content.strip()
        session_memory[sender_id].append({"role": "assistant", "content": reply_text})
        return reply_text
    except Exception as e:
        print("❌ Error:", e)
        return "⚠ في مشكلة تقنية، جرب تبعت تاني بعد شوية."

def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {
        "Content-Type": "application/json",
        "Client-Token": CLIENT_TOKEN
    }
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        return response.json()
    except Exception as e:
        print("❌ ZAPI Error:", e)
        return {"status": "error", "message": str(e)}

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

    if msg and sender:
        reply = ask_chatgpt(msg, sender)
        send_message(sender, reply)

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

