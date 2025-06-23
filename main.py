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

# 🧠 أدوات ذكية
def detect_link(text):
    return "http" in text or "facebook.com" in text or "tiktok.com" in text or "instagram.com" in text

def detect_payment_text(text):
    keywords = ["حولت", "تم التحويل", "الفلوس", "دفعت", "دفعتلك", "صورة التحويل", "السكرين"]
    return any(word in text.lower() for word in keywords)

def match_service(text):
    for s in services:
        if s["platform"].lower() in text.lower() and str(s["count"]) in text:
            return s
    return None

def build_price_prompt():
    return "\n".join([
        f"- {s['platform']} | {s['type']} | {s['count']} = {s['price']} جنيه ({s['audience']})"
        for s in services
    ])

def ask_chatgpt(message, sender_id):
    session = get_session(sender_id)
    history = session["history"]

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
        save_session(sender_id, history, session["status"])
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
    message_type = data.get("type")  # image / text / etc

    session = get_session(sender)
    status = session["status"]

    # 🎯 التعامل مع صورة تحويل حقيقية
    if message_type == "image" and status == "waiting_payment":
        session["status"] = "completed"
        save_session(sender, session["history"], session["status"])
        send_message(sender, replies["تأكيد_التحويل"])
        return jsonify({"status": "payment_confirmed"}), 200

    # 🧩 صورة لكن مش مطلوب دفع
    if message_type == "image" and status != "waiting_payment":
        send_message(sender, replies["صورة_غير_مفهومة"])
        return jsonify({"status": "image_ignored"}), 200

    # 🛒 العميل طلب خدمة
    service = match_service(msg)
    if service and status == "idle":
        session["status"] = "waiting_link"
        session["history"].append({"role": "user", "content": msg})
        save_session(sender, session["history"], session["status"])
        send_message(sender, replies["طلب_الرابط"].format(price=service["price"]))
        return jsonify({"status": "service_matched"}), 200

    # 🔗 العميل بعت رابط
    if detect_link(msg) and status == "waiting_link":
        session["status"] = "waiting_payment"
        session["history"].append({"role": "user", "content": msg})
        save_session(sender, session["history"], session["status"])
        send_message(sender, replies["طلب_الدفع"])
        return jsonify({"status": "link_received"}), 200

    # 💸 العميل قال إنه حول
    if detect_payment_text(msg) and status == "waiting_payment":
        session["status"] = "completed"
        session["history"].append({"role": "user", "content": msg})
        save_session(sender, session["history"], session["status"])
        send_message(sender, replies["تأكيد_التحويل"])
        return jsonify({"status": "text_payment_confirmed"}), 200

    # ✅ لو كل ده مش حاصل نرجع لـ GPT
    reply = ask_chatgpt(msg, sender)
    send_message(sender, reply)
    return jsonify({"status": "replied"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
