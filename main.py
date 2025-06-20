import os
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from openai import OpenAI

# تحميل المتغيرات البيئية من ملف env.
load_dotenv()

# إعداد مفاتيح API والمتغيرات
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = "https://openai.chatgpt4mena.com/v1"
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")

app = Flask(_name_)
session_memory = {}

# تهيئة عميل OpenAI
client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_API_BASE
)

# دالة بناء prompt للأسعار (يمكنك تعديلها لتناسب خدماتك)
def build_price_prompt():
    return "خدماتنا تشمل تزويد المتابعين، إعلانات ممولة، والاشتراكات الشهرية (5k/10k - مصري وعربي)."

# دالة التواصل مع ChatGPT
def ask_chatgpt(message, sender_id):
    if sender_id not in session_memory:
        session_memory[sender_id] = [
            {
                "role": "system",
"content": f"""أنت بوت مساعد ودود باللهجة المصرية لصفحة Followers Store. 
مهمتك الرد على استفسارات العملاء، وبيع الخدمات، وجمع الاشتراكات، وتحويل المحادثات للموظفين عند الحاجة.
إذا لم تتمكن من الإجابة على سؤال، أو كان السؤال غير واضح، قم بالرد بعبارة 'تحويل لموظف' فقط.

معلومات عن خدماتنا:
{build_price_prompt()}"""
                {build_price_prompt()}"
            }
        ]

    # إضافة رسالة المستخدم إلى ذاكرة المحادثة
    session_memory[sender_id].append({"role": "user", "content": message})
    print("OPENAI_API_KEY being used:", OPENAI_API_KEY)  # للمراجعة والتحقق

    try:
        response = client.chat.completions.create(
            model="gpt-4",  # تأكد أن هذا الموديل متاح في خدمتك
            messages=session_memory[sender_id],
            max_tokens=400  # عدد الكلمات الأقصى للرد
        )
        data = response.model_dump()  # تحويل الرد لنطبع كل حاجة
        print("🌀 GPT raw response:", data)

        # استخراج الرد من OpenAI
        if "choices" in data and "message" in data["choices"][0]:
            reply_text = data["choices"][0]["message"]["content"].strip()

            # إضافة رد البوت لذاكرة المحادثة
            session_memory[sender_id].append({"role": "assistant", "content": reply_text})
            return reply_text
        else:
            return "⚠ حصلت مشكلة في توليد الرد من الذكاء الاصطناعي. جرب تاني بعد شوية."
    except Exception as e:
        print("❌ Exception during OpenAI call:", e)
        return "⚠ في مشكلة تقنية حالياً مع الذكاء الاصطناعي. إبعتلي تاني بعد شوية"

# دالة إرسال الرسائل عبر ZAPI
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
        print("ZAPI Response:", zapi_response_data)  # للمراجعة والتحقق
        return zapi_response_data
    except Exception as e:
        print("❌ Exception during ZAPI send_message:", e)
        return {"status": "error", "message": f"ZAPI: فشل إرسال الرسالة عبر: {e}"}

# Webhook route لاستقبال رسائل واتساب
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "✅ Webhook 200 جاهز", 200

    print("✅ Webhook تم استدعاؤه")
    data = request.json  # البيانات المستلمة من ZAPI
    print("📦 البيانات المستلمة:")
    print(data)

    incoming_msg = None
    sender = None

    # محاولة استخراج الرسالة ورقم المرسل من JSON payload
    if data and "text" in data and "message" in data["text"]:
        incoming_msg = data["text"]["message"]
    elif data and "body" in data:
        incoming_msg = data["body"]

    if data and "phone" in data:
        sender = data["phone"]
    elif data and "From" in data:
        sender = data["From"]

    if incoming_msg and sender:
        print(f"✉ رسالة من {sender} - {incoming_msg}")
        reply = ask_chatgpt(incoming_msg, sender)

        # منطق التحويل لموظف بشري
        if "تحويل لموظف" in reply:
            send_message(sender, "عزيزي، لم أتمكن من فهم طلبك بشكل كامل أو أن طلبك يحتاج لتدخل بشري. سيتم تحويل محادثتك الآن إلى أحد ممثلي خدمة العملاء، وسيتواصل معك في أقرب وقت ممكن.")
            print(f"⚠ تم تحويل المحادثة لـ {sender} إلى موظف بشري.")
        else:
            # إرسال الرد العادي للعميل
            send_message(sender, reply)
    else:
        print("⚠ البيانات غير مكتملة أو غير متوقعة. Received data:", data)

    return jsonify({"status": "received"}), 200

# Home route للتأكد إن البوت شغال
@app.route("/")
def home():
    return "✅ البوت شغال"

if _name_ == "_main_":
    app.run(host="0.0.0.0", port=5000)
