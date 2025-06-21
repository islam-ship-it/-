from flask import Flask, request, jsonify
import openai
import os
import requests

app = Flask(__name__)

# إعداد مفاتيح API
openai.api_key = os.getenv("OPENAI_API_KEY")
openai.api_base = "https://openai.chatgpt4mena.com/v1"

ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")

# ذاكرة المحادثة
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
        print(f"[ZAPI] أُرسلت إلى {phone_number}: {response.status_code}")
        print(f"[ZAPI] رد السيرفر: {response.text}")
        return response.status_code == 200
    except Exception as e:
        print(f"[ZAPI] فشل الإرسال: {e}")
        return False

# الصفحة الرئيسية
@app.route('/')
def home():
    return '✅ البوت شغال! استخدم /webhook لاستقبال الرسائل.'

# نقطة الاستقبال من ZAPI
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or request.form.to_dict() or {}
        print("[Webhook] البيانات المستلمة:", data)

        # استخراج الرقم والرسالة من الهيكل الجديد
        phone_number = (
            data.get("messageData", {}).get("senderData", {}).get("chatId") or
            data.get("chatId") or
            data.get("phone")
        )

        message = (
            data.get("messageData", {}).get("textMessageData", {}).get("textMessage") or
            data.get("message") or
            data.get("text")
        )

        # معالجة الرقم (لو فيه @ مثلاً)
        if phone_number and "@" in phone_number:
            phone_number = phone_number.split("@")[0]

        print(f"[📥] رقم: {phone_number} | رسالة: {message}")

        if not phone_number or not message:
            return jsonify({"error": "بيانات غير مكتملة"}), 400

        # حفظ المحادثة
        history = session_memory.get(phone_number, [])
        history.append({"role": "user", "content": message})
        session_memory[phone_number] = history[-10:]

        chat_response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "أنت بوت بترد باللهجة المصرية على استفسارات العملاء بشكل محترم وسلس."},
                *session_memory[phone_number]
            ]
        )

        reply = chat_response.choices[0].message.content
        session_memory[phone_number].append({"role": "assistant", "content": reply})
        send_whatsapp_message(phone_number, reply)
        return jsonify({"status": "ok"}), 200

   except Exception as e:
    print("[❌ ERROR أثناء الرد]:", e)
    return jsonify({"error": str(e)}), 500

# تشغيل السيرفر
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=10000)
