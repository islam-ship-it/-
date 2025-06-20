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
last_order = {}

client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)

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

def ask_chatgpt(message, sender_id):
    confirm_text = replies["تأكيد_الطلب"]

    # تحليل نوع الطلب (لو فيه طلب سابق)
    previous_order = last_order.get(sender_id, "")
    link_hint = ""

    if previous_order:
        if "متابع" in previous_order:
            link_hint = "نوع الخدمة: متابعين ➜ اطلب من العميل رابط الصفحة."
        elif "لايك" in previous_order:
            link_hint = "نوع الخدمة: لايكات ➜ اطلب من العميل رابط البوست."
        elif "مشاهدة" in previous_order:
            link_hint = "نوع الخدمة: مشاهدات ➜ اطلب من العميل رابط الفيديو."
        elif "تعليق" in previous_order:
            link_hint = "نوع الخدمة: تعليقات ➜ اطلب من العميل رابط البوست أو الفيديو."
        elif "اشتراك" in previous_order:
            link_hint = "نوع الخدمة: اشتراكات ➜ اطلب من العميل رابط القناة."
        elif "تيليجرام" in previous_order or "تليجرام" in previous_order:
            link_hint = "نوع الخدمة: تفاعلات تيليجرام ➜ اطلب من العميل رابط الجروب أو القناة."

    # إضافة تعليمات الرابط في نهاية البرومبت
    system_prompt = static_prompt.format(
        prices=build_price_prompt(),
        confirm_text=confirm_text + ("\n\n📌 تنبيه بناءً على التحليل:\n" + link_hint if link_hint else "")
    )

    session_memory[sender_id] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message}
    ]

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=session_memory[sender_id],
            max_tokens=400
        )
        data = response.model_dump()

        if "choices" in data and data["choices"] and "message" in data["choices"][0]:
            reply_text = data["choices"][0]["message"]["content"].strip()

            if any(word in message for word in ["متابع", "لايك", "مشاهدة", "تعليق", "اشتراك", "تيليجرام"]) and any(char.isdigit() for char in message):
                last_order[sender_id] = message

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
        confirmation_keywords = ["تمام", "كمل", "عايز أكمل", "ايه المطلوب", "ابدأ", "أيوه"]
        if any(word in incoming_msg.lower() for word in confirmation_keywords):
            last = last_order.get(sender, "")
            if last:
                incoming_msg = f"العميل قال إنه عايز يكمل، وكان طالب قبل كده: {last}"
            else:
                incoming_msg = "العميل قال تمام بس مفيش طلب محفوظ، فتعامل طبيعي."

        reply = ask_chatgpt(incoming_msg, sender)

        if "تحويل لموظف" in reply:
            send_message(sender, "عزيزي العميل، لم أتمكن من فهم طلبك بشكل دقيق. سيتم تحويلك الآن لممثل خدمة عملاء.")
        else:
            send_message(sender, reply)

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
    
