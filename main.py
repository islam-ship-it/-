import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from openai import OpenAI

# استيراد ملفاتك الخاصة بالبيانات والتعليمات
from static_replies import static_prompt, replies
from services_data import services

# حمل المتغيرات البيئية من ملف .env
load_dotenv()

# تعريف المتغيرات البيئية
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = "https://openai.chatgpt4mena.com/v1"

ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

app = Flask(__name__)
session_memory = {}

# تهيئة عميل OpenAI
client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_API_BASE
)

# دالة بناء الأسعار
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

# داخل ask_chatgpt
def ask_chatgpt(message, sender_id):
    print(f"DEBUG: Type of replies: {type(replies)}")
    print(f"DEBUG: Content of replies: {replies}")
    print(f"DEBUG: Type of static_prompt: {type(static_prompt)}")
    print(f"DEBUG: Content of static_prompt (first 200 chars): {static_prompt[:200]}")

    confirm_text = replies["تأكيد_الطلب"]

    session_memory[sender_id] = [
        {
            "role": "system",
            "content": static_prompt.format(
                prices=build_price_prompt(),
                confirm_text=confirm_text
            )
        }
    ]

    session_memory[sender_id].append({"role": "user", "content": message})
    print("✅ OPENAI_API_KEY:", OPENAI_API_KEY)

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=session_memory[sender_id],
            max_tokens=400
        )
        data = response.model_dump()
        print("🤖 GPT raw response:", data)

        if "choices" in data and data["choices"] and "message" in data["choices"][0]:
            reply_text = data["choices"][0]["message"]["content"].strip()
            session_memory[sender_id].append({"role": "assistant", "content": reply_text})
            return reply_text
        else:
            return "⚠ حصلت مشكلة في توليد الرد. جرب تاني بعد شوية."
    except Exception as e:
        print("❌ Exception:", e)
        return "⚠ في مشكلة تقنية مع الذكاء الاصطناعي. جرب تاني بعد شوية."

# إرسال رسالة على ZAPI
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
        data = response.json()
        print("✅ تم إرسال الرد:", data)
        return data
    except Exception as e:
        print("❌ ZAPI Error:", e)
        return {"status": "error", "message": str(e)}

# الصفحة الرئيسية
@app.route("/")
def home():
    return "✅ البوت شغال"

# Webhook
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "✅ Webhook جاهز", 200

    data = request.json
    print("📩 البيانات المستلمة:", data)

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
        print(f"📨 رسالة من {sender}: {incoming_msg}")
        reply = ask_chatgpt(incoming_msg, sender)

        if "تحويل لموظف" in reply:
            send_message(sender, "عزيزي العميل، لم أتمكن من فهم طلبك بشكل دقيق. سيتم تحويلك الآن لممثل خدمة عملاء.")
        else:
            send_message(sender, reply)
    else:
        print("⚠ بيانات غير مكتملة:", data)

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
