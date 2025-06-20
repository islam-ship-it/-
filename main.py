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

def ask_chatgpt(message, sender_id):
    if sender_id not in session_memory:
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
    print("OPENAI_API_KEY being used:", OPENAI_API_KEY)

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=session_memory[sender_id],
            max_tokens=400
        )
        data = response.model_dump()
        print("📤 GPT raw response:", data)

        if "choices" in data and "message" in data["choices"][0]:
            reply_text = data["choices"][0]["message"]["content"].strip()
            session_memory[sender_id].append({"role": "assistant", "content": reply_text})
            return reply_text
        else:
            return "⚠ حصلت مشكلة في توليد الرد من الذكاء الاصطناعي. جرب تاني بعد شوية."
    except Exception as e:
        print("❌ Exception during OpenAI call:", e)
        return "⚠ في مشكلة تقنية حالياً مع الذكاء الاصطناعي. إبعتلي تاني بعد شوية"

def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    payload = {
        "phone": phone,
        "message": message
    }
    headers = {
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        zapi_response_data = response.json()
        print("✅ تم إرسال الرد إلى العميل.")
        print("ZAPI Response:", zapi_response_data)
        return zapi_response_data
    except Exception as e:
        print("❌ Exception during ZAPI send_message:", e)
        return {"status": "error", "message": f"فشل إرسال الرسالة عبر ZAPI: {e}"}

@app.route("/")
def home():
    return "✅ البوت شغال"

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "✅ Webhook 200 جاهز"

    print("✅ تم استدعاء Webhook")
    data = request.json
    print("📦 البيانات المستلمة:")
    print(data)

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
        print(f"💌 رسالة من: {sender} - {incoming_msg}")
        reply = ask_chatgpt(incoming_msg, sender)

        if "تحويل لموظف" in reply:
            send_message(sender, "عزيزي، لم أتمكن من فهم طلبك بشكل كامل أو أن طلبك يحتاج لتدخل بشري. سيتم تحويل محادثتك الآن إلى أحد ممثلي خدمة العملاء، وسيتواصل معك في أقرب وقت ممكن.")
            print(f"⚠ تم تحويل المحادثة لـ {sender} إلى موظف بشري")
        else:
            send_message(sender, reply)
    else:
        print("⚠ البيانات غير مكتملة أو غير متوقعة. Received data:", data)

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
