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

def determine_link_type(service_type):
    if "متابع" in service_type:
        return "رابط الصفحة"
    elif "لايك" in service_type:
        return "رابط البوست"
    elif "مشاهدة" in service_type or "فيديو" in service_type:
        return "رابط الفيديو"
    else:
        return "الرابط المناسب"

def extract_services_from_message(message):
    extracted = []
    for item in services:
        if str(item["count"]) in message and item["type"] in message and item["platform"] in message:
            extracted.append(item)
    return extracted

def generate_link_request_text(services_requested):
    lines = []
    for service in services_requested:
        link_type = determine_link_type(service["type"])
        line = f"📎 ابعت {link_type} الخاصة بـ {service['count']} {service['type']} {service['platform']}"
        lines.append(line)
    return "\n".join(lines)

def ask_chatgpt(message, sender_id):
    if sender_id not in session_memory:
        session_memory[sender_id] = []

    session_memory[sender_id].append({"role": "user", "content": message})

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
    return reply


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)  # force=True عشان نضمن القراءة
        print("🔥 RAW JSON:", data)

        return jsonify({"msg": "received"}), 200

    except Exception as e:
        print("❌ Error:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
