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
            model="gpt-4o",
            messages=session_memory[sender_id],
            max_tokens=400
        )
        data = response.model_dump()
        if "choices" in data and data["choices"] and "message" in data["choices"][0]:
            reply_text = data["choices"][0]["message"]["content"].strip()
            session_memory[sender_id].append({"role": "assistant", "content": reply_text})

            if any(kw in reply_text for kw in ["علشان نكمل الطلب", "ابعتلنا روابط", "محتاجين منك"]):
                requested_services = extract_services_from_message(message)
                if requested_services:
                    links_text = generate_link_request_text(requested_services)
                    reply_text = f"{links_text}\n\n{replies['الدفع']}"
                    session_memory[sender_id].append({"role": "assistant", "content": reply_text})

            return reply_text
        else:
            return "⚠ حصلت مشكلة في توليد الرد. جرب تاني بعد شوية."
    except Exception as e:
        return "⚠ في مشكلة تقنية مع الذكاء الاصطناعي. جرب تاني بعد شوية."

def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {
        "Content-Type": "application/json",
        "Client-Token": CLIENT_TOKEN
    }
    payload = {
        "phone": phone,
        "message": message
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        return response.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.route("/")
def home():
    return "✅ البوت شغال"

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "✅ Webhook جاهز", 200

    data = request.json
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
        reply = ask_chatgpt(incoming_msg, sender)
        send_message(sender, reply)

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
