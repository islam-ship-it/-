import os
import re
import time
import requests
from flask import Flask, request, jsonify
from openai import OpenAI

from static_replies import static_prompt, replies
from services_data import services
from session_storage import get_session, save_session
from intent_handler import detect_intent as analyze_intent
from rules_engine import apply_rules
from link_validator import is_valid_service_link
from message_classifier import classify_message_type
from bot_control import is_bot_active
from model_selector import choose_model
from message_buffer import add_to_buffer

# إعدادات OpenRouter
OPENAI_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENAI_API_BASE = "https://api.openai.com/v1"
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
    session = get_session(sender_id)

    if not session["history"]:
        session["history"].append({
            "role": "system",
            "content": static_prompt.format(
                prices=build_price_prompt(),
                confirm_text=replies["تأكيد_الطلب"]
            )
        })

    session["history"].append({"role": "user", "content": message})

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=session["history"][-10:],
            max_tokens=500
        )
        reply_text = response.choices[0].message.content.strip()
        session["history"].append({"role": "assistant", "content": reply_text})
        save_session(sender_id, session)
        return reply_text
    except Exception as e:
        print("❌ GPT Error:", e)
        return "⚠ في مشكلة تقنية مؤقتة. جرب تبعت تاني كمان شوية."

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
    media_type = data.get("type", "text")

    if not sender:
        return jsonify({"status": "no sender"}), 400

    # التحقق من حالة البوت
    if not is_bot_active(sender):
        return jsonify({"status": "bot inactive"}), 200

    # تجميع الرسائل القصيرة المتتالية
    full_message = add_to_buffer(sender, msg)
    if not full_message:
        return jsonify({"status": "buffering"}), 200

    # تصنيف نوع الرسالة
    message_type = classify_message_type(full_message)

    # تحليل النية
    session = get_session(sender)
    intent = analyze_intent(full_message, session, message_type)
    print(f"📌 Intent: {intent}")

    # تطبيق القواعد الذكية
    response = apply_rules(
        message=full_message,
        intent=intent,
        session=session,
        services=services,
        replies=replies
    )

    # اختيار النموذج المناسب
    matched_services = session.get("matched_services", [])
    model, reason = choose_model(full_message, matched_services)
    print(f"✅ Using model: {model} → {reason}")

    # حفظ الجلسة
    save_session(sender, session)

    # إرسال الرد
    send_message(sender, response)
    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
