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


def detect_link(text):
    return any(x in text.lower() for x in ["http", "facebook.com", "instagram.com", "tiktok.com", "youtu", "www."])


def detect_payment(text):
    payment_keywords = ["حولت", "تم الدفع", "تم التحويل", "دفعت", "حول", "الفلوس", "رصيد", "صورة التحويل"]
    return any(word in text.lower() for word in payment_keywords)


def detect_image(message_type):
    return message_type == "image"


def match_service(text):
    for s in services:
        if s["platform"].lower() in text.lower() and str(s["count"]) in text:
            return s
    return None


def validate_service_link(service, link):
    if not service or not link:
        return True

    platform = service["platform"].lower()
    type_ = service["type"].lower()

    if platform == "فيسبوك":
        if type_ == "متابعين" and "/posts/" in link:
            return False
        if type_ in ["لايكات", "مشاهدات"] and not any(x in link for x in ["/posts/", "/videos/"]):
            return False
    elif platform == "انستجرام":
        if type_ == "متابعين" and "/p/" in link:
            return False
        if type_ in ["لايكات", "مشاهدات"] and not "/p/" in link:
            return False
    elif platform == "تيك توك":
        if type_ == "متابعين" and "/video/" in link:
            return False
        if type_ in ["مشاهدات", "لايكات"] and not "/video/" in link:
            return False
    elif platform == "يوتيوب":
        if type_ == "مشتركين" and "watch?v=" in link:
            return False
        if type_ in ["مشاهدات", "لايكات"] and not "watch?v=" in link:
            return False
    return True


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
            model="gpt-4.1",
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

    session = get_session(sender)
    status = session.get("status", "idle")

    # ✅ 1. صورة دفع أثناء "waiting_payment"
    if detect_image(media_type) and status == "waiting_payment":
        session["status"] = "completed"
        save_session(sender, session)
        send_message(sender, replies["تأكيد_التحويل"])
        return jsonify({"status": "received"}), 200

    # ✅ 2. صورة في غير مرحلة الدفع
    if detect_image(media_type):
        send_message(sender, replies["صورة_غير_مفهومة"])
        return jsonify({"status": "received"}), 200

    # ✅ 3. استفسار عن خدمة جديدة
    matched = match_service(msg)
    if matched and status in ["idle", "completed"]:
        session["status"] = "waiting_link"
        session["selected_service"] = matched
        session["history"] = [{"role": "user", "content": msg}]
        save_session(sender, session)
        send_message(sender, replies["طلب_الرابط"].format(price=matched["price"]))
        return jsonify({"status": "received"}), 200

    # ✅ 4. رابط بعد اختيار الخدمة
    if detect_link(msg) and status == "waiting_link":
        matched_service = session.get("selected_service")
        if validate_service_link(matched_service, msg):
            session["status"] = "waiting_payment"
            session["history"].append({"role": "user", "content": msg})
            save_session(sender, session)
            send_message(sender, replies["طلب_الدفع"])
        else:
            send_message(sender, replies["رابط_غير_صحيح"])
        return jsonify({"status": "received"}), 200

    # ✅ 5. تأكيد دفع نصي
    if detect_payment(msg) and status == "waiting_payment":
        session["status"] = "completed"
        session["history"].append({"role": "user", "content": msg})
        save_session(sender, session)
        send_message(sender, replies["تأكيد_التحويل"])
        return jsonify({"status": "received"}), 200

    # ✅ 6. أي حالة أخرى: رد من GPT
    reply = ask_chatgpt(msg, sender)
    send_message(sender, reply)

    return jsonify({"status": "received"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
