from flask import Flask, request, jsonify
import openai
import os
import requests

app = Flask(__name__)

# إعداد مفاتيح البيئة
openai.api_key = os.getenv("OPENAI_API_KEY")
openai.api_base = "https://openai.chatgpt4mena.com/v1"  # رابط API المخصص
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")

# ذاكرة الجلسة
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
        print(f"[ZAPI] خطأ في الإرسال: {e}")
        return False

# Webhook
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    print(f"[Webhook] البيانات: {data}")

    phone_number = data.get("phone")
    message = data.get("message")

    if not phone_number or not message:
        return jsonify({"error": "Missing phone or message"}), 400

    # حفظ الرسائل السابقة
    history = session_memory.get(phone_number, [])
    history.append({"role": "user", "content": message})
    session_memory[phone_number] = history[-10:]

    try:
        chat_response = openai.ChatCompletion.create(
            model="gpt-4o",  # ← النموذج الجديد السريع والدقيق
            messages=[
                {"role": "system", "content": "أنت مساعد ذكي تتحدث باللهجة المصرية وتجاوب على استفسارات العملاء بطريقة ودية ومنظمة."},
                *session_memory[phone_number]
            ]
        )
        reply = chat_response.choices[0].message.content
        session_memory[phone_number].append({"role": "assistant", "content": reply})
        send_whatsapp_message(phone_number, reply)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"[OpenAI] خطأ: {e}")
        return jsonify({"error": "حدث خطأ داخلي"}), 500

# تشغيل السيرفر
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=10000)
