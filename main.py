from flask import Flask, request, jsonify
import requests
import os
import openai

app = Flask(__name__)

# OpenAI & ZAPI credentials
openai.api_key = os.getenv("OPENAI_API_KEY")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

# Static system prompt
STATIC_PROMPT = """
انت بوت واتساب شغال بتساعد العملاء باللهجة المصرية، وظيفتك ترد على استفساراتهم عن الأسعار والخدمات الخاصة بتزويد متابعين، لايكات، تعليقات، مشاهدات، اشتراكات ChatGPT، إعلانات ممولة، صفحات، وهكذا. 
ردودك تكون ودودة، واقعية، ومنظمة بإيموجي، وما تكررش الكلام. لو العميل وافق على السعر، اطلب منه رابط الصفحة أو الفيديو حسب نوع الخدمة.
"""

# Session memory لكل عميل
session_memory = {}

# إرسال رسالة واتساب
def send_whatsapp_message(phone_number, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    payload = {
        "phone": phone_number,
        "message": message
    }
    try:
        response = requests.post(url, json=payload)
        print(f"[ZAPI] إرسال الرسالة لـ {phone_number}: {response.status_code}")
        print(f"[ZAPI] الرد من ZAPI: {response.text}")
        return response.status_code == 200
    except Exception as e:
        print(f"[خطأ في ZAPI]: {e}")
        return False

# استقبال الويب هوك
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print(f"[📩] البيانات المستلمة: {data}")

        if not data or 'message' not in data:
            print("[⚠] مفيش رسالة داخلية في البيانات")
            return jsonify({"error": "Invalid payload"}), 400

        message_data = data['message']
        phone = message_data.get("from")
        message_text = message_data.get("body")

        if not phone or not message_text:
            print("[⚠] مفيش رقم أو نص الرسالة")
            return jsonify({"error": "Missing phone or message"}), 400

        print(f"[👤] العميل: {phone}")
        print(f"[📝] الرسالة: {message_text}")

        # ذاكرة المحادثة
        history = session_memory.get(phone, [])
        history.append({"role": "user", "content": message_text})

        # استدعاء ChatGPT
        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": STATIC_PROMPT},
                *history
            ]
        )

        reply = response.choices[0].message["content"].strip()
        print(f"[🤖] رد ChatGPT: {reply}")

        if not reply:
            print("[⚠] الرد فاضي!")
            reply = "حصلت مشكلة في الرد، جرب تبعت تاني 🙏"

        history.append({"role": "assistant", "content": reply})
        session_memory[phone] = history[-10:]  # آخر 10 رسائل فقط

        # إرسال الرد عبر واتساب
        success = send_whatsapp_message(phone, reply)
        if not success:
            print("[🚫] فشل إرسال الرد للعميل.")
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"[❌] خطأ أثناء المعالجة: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return "🤖 بوت واتساب شغال تمام ✅", 200

if __name__ == "__main__":
    app.run(debug=False, port=10000)
