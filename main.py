import os
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from services_data import services
from prompt import static_prompt
from replies import replies

# --- إعداد البيئة
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE")

app = Flask(__name__)
session_memory = {}

# --- تهيئة OpenAI
client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_API_BASE
)

# --- تجهيز البيانات في شكل نص prompt
def build_price_prompt():
    lines = []
    for item in services:
        line = f"- {item['platform']} | {item['type']} | {item['count']} = {item['price']} جنيه ({item['audience']})"
        lines.append(line)
    return "\n".join(lines)

# --- الدالة اللي بتكلم ChatGPT
def ask_chatgpt(message, sender_id):
    if sender_id not in session_memory:
        session_memory[sender_id] = [
            {
                "role": "system",
                "content": static_prompt.format(
                    prices=build_price_prompt(),
                    confirm_text=replies["تأكيد الطلب"]
                )
            }
        ]
    session_memory[sender_id].append({"role": "user", "content": message})

    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=session_memory[sender_id]
    )

    reply = response.choices[0].message.content
    print(f"✅ رد من ChatGPT: {reply}")  # سطر مهم جدًا للمراقبة
    session_memory[sender_id].append({"role": "assistant", "content": reply})
    return reply

# --- Webhook الأساسي
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    incoming_msg = None
    sender = None

    if "text" in data and "message" in data["text"]:
        incoming_msg = data["text"]["message"]
    elif "body" in data:
        incoming_msg = data["body"]

    if "phone" in data:
        sender = data["phone"]
    elif "From" in data:
        sender = data["From"]

    if incoming_msg and sender:
        print(f"📥 رسالة جاية من {sender}: {incoming_msg}")
        reply = ask_chatgpt(incoming_msg, sender)

        # إرسال الرد باستخدام ZAPI
        requests.post(
            f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text",
            json={"to": sender, "message": reply}
        )

        return jsonify({"status": "sent"}), 200

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=10000)
