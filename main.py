import os
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from static_replies import static_prompt, replies
from services_data import services
from session_storage import get_session, save_session, reset_session

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

def ask_chatgpt(message, sender_id):
    history = get_session(sender_id)
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
        save_session(sender_id, history)
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


def detect_link(text):
    return "http" in text or "www." in text or "tiktok.com" in text or "facebook.com" in text

def detect_payment(text):
    payment_keywords = ["تم التحويل", "حولت", "الفلوس", "وصل", "سكرين", "صورة"]
    return any(word in text.lower() for word in payment_keywords)

def detect_image(message_type):
    return message_type == "image"

def match_service(text):
    for s in services:
        if s["platform"].lower() in text.lower() and str(s["count"]) in text:
            return s
    return None

def smart_reply_logic(text, sender_id, message_type="text"):
    session = get_session(sender_id)
    status = session.get("status", "idle")

    if detect_image(message_type) and status == "waiting_payment":
        session["status"] = "completed"
        save_session(sender_id, session)
        return replies.get("تأكيد_التحويل", "✅ تم استلام التحويل، هنبدأ التنفيذ فورًا.")

    if detect_image(message_type):
        return replies.get("صورة_غير_مفهومة", "📷 تم استلام صورة، من فضلك وضح محتواها.")

    if status == "idle":
        service = match_service(text)
        if service:
            session["status"] = "waiting_link"
            session["last_price"] = service["price"]
            save_session(sender_id, session)
            return replies.get("طلب_الرابط", "🔗 من فضلك ابعت الرابط").format(price=service["price"])

    if detect_link(text) and status == "waiting_link":
        session["status"] = "waiting_payment"
        save_session(sender_id, session)
        return replies.get("طلب_الدفع", "💰 تمام، ابعت صورة التحويل هنبدأ فورًا")

    if detect_payment(text) and status == "waiting_payment":
        session["status"] = "completed"
        save_session(sender_id, session)
        return replies.get("تأكيد_التحويل", "✅ تم استلام التحويل، هنبدأ التنفيذ فورًا.")

    return None


def webhook():
    if request.method == "GET":
        return "✅ Webhook جاهز", 200

    data = request.json
    msg = data.get("text", {}).get("message") or data.get("body", "")
    sender = data.get("phone") or data.get("From")

    
    if msg and sender:
        smart = smart_reply_logic(msg, sender, media_type)
        if smart:
            send_message(sender, smart)
            return jsonify({"status": "smart"}), 200

        reply = ask_chatgpt(msg, sender)
        send_message(sender, reply)

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

