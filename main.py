import os
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from session_storage import get_session, save_session
from services_data import services

# إعدادات البيئة
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = "https://api.openai.com/v1"
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

app = Flask(__name__)

client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)

# دالة جلب العروض من ملف الخدمات
def get_service_offers(platform, service_type):
    offers = []
    for item in services:
        if item["platform"].lower() == platform.lower() and item["type"].lower() == service_type.lower():
            offer = f'{item["count"]} {service_type} بـ {item["price"]} ج - {item["audience"]}'
            offers.append(offer)
    return offers

# الرد على الرسالة
def ask_chatgpt(message, sender_id):
    session = get_session(sender_id)

    if not any(msg["role"] == "system" for msg in session["history"]):
        session["history"].insert(0, {
            "role": "system",
            "content": "أنت مساعد ذكي، ترد على عملاء متجر المتابعين باللهجة المصرية، هدفك تقفل الطلب وتساعد العميل بسرعة وباحتراف، لازم تعرض العروض والأسعار لما العميل يطلب، وتختم كل رد بسؤال ذكي."})

    session["history"].append({"role": "user", "content": message})

    # استجابة مخصصة للعروض لو السؤال فيه أسعار أو عروض
    if "أسعار" in message or "عرض" in message or "العروض" in message:
        offers = get_service_offers("تيك توك", "متابع") + get_service_offers("إنستجرام", "لايك")
        if offers:
            reply = "دي العروض المتاحة:
" + "\n".join(offers) + "\nتحب نبدأ بكم متابع أو لايك؟"
        else:
            reply = "العروض حالياً مش متاحة، ابعتلي الخدمة المطلوبة أو المنصة وانا أجهزلك التفاصيل."
    else:
        try:
            raw_response = client.chat.completions.create(
                model="ft:gpt-4.1-2025-04-14:boooot-waaaatsaaap:BotJ0nCz",
                messages=session["history"][-10:],
                max_tokens=500
            )
            reply = raw_response.choices[0].message.content.strip()
        except Exception as e:
            print("❌ GPT Error:", e)
            reply = "⚠ في مشكلة تقنية مؤقتة. جرب تبعت تاني كمان شوية."

    session["history"].append({"role": "assistant", "content": reply})
    save_session(sender_id, session)
    return reply

# إرسال رسالة عبر ZAPI
def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
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

    if not sender:
        return jsonify({"status": "no sender"}), 400

    reply = ask_chatgpt(msg, sender)
    send_message(sender, reply)
    return jsonify({"status": "sent"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
