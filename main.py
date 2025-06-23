import os
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from static_replies import static_prompt, replies
from services_data import services
from session_storage import get_session, save_session

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = "https://openrouter.ai/api/v1"
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)

def build_price_prompt():
    return "\n".join([
        f"- {s['platform']} | {s['type']} | {s['count']} = {s['price']} جنيه ({s['audience']})"
        for s in services
    ])

def parse_number(text):
    text = text.replace("ألف", "000").replace("٠", "0").replace("١", "1").replace("٢", "2").replace("٣", "3").replace("٤", "4").replace("٥", "5").replace("٦", "6").replace("٧", "7").replace("٨", "8").replace("٩", "9")
    numbers = ''.join([c if c.isdigit() else ' ' for c in text])
    numbers = numbers.strip().split()
    if numbers:
        return int(numbers[0])
    return None

def match_service(message):
    msg = message.lower()
    count = parse_number(msg)
    if not count:
        return None

    for s in services:
        if s['count'] != count:
            continue
        if s['platform'].lower() in msg and s['type'].lower() in msg:
            return s
        if s['platform'].lower() in msg and any(word in msg for word in [s['type'].lower(), s['type'].lower() + "ات"]):
            return s
    return None

def ask_chatgpt(message, sender_id):
    session = get_session(sender_id)
    history = session.get("history", [])
    status = session.get("status", "idle")

    if not history:
        history.append({
            "role": "system",
            "content": static_prompt.format(
                prices=build_price_prompt(),
                confirm_text=replies["تأكيد_الطلب"]
            )
        })

    history.append({"role": "user", "content": message})

    try:
        response = client.chat.completions.create(
            model="gpt-4.1",
            messages=history[-10:],
            max_tokens=500
        )
        reply_text = response.choices[0].message.content.strip()
        history.append({"role": "assistant", "content": reply_text})
        save_session(sender_id, {"history": history, "status": status})
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
