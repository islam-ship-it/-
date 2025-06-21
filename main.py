import os
print("🚀 BOT STARTED - VERSION CHECK")
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

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    user_msg = data.get("message", "")
    sender_id = data.get("from", "")

    if not user_msg or not sender_id:
        return jsonify({"error": "Invalid payload"}), 400

    if sender_id not in session_memory:
        session_memory[sender_id] = []

    session_memory[sender_id].append({"role": "user", "content": user_msg})

    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": static_prompt.format(
                prices=build_price_prompt(),
                confirm_text=replies["تأكيد_الطلب"]
            )},
            *session_memory[sender_id]
        ]
    )

    reply = response.choices[0].message.content
    session_memory[sender_id].append({"role": "assistant", "content": reply})

    requests.post(
        f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text",
        json={"to": sender_id, "message": reply}
    )

    return jsonify({"status": "sent"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
