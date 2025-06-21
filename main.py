import os
print("🚀 BOT STARTED - VERSION CHECK")
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from openai import OpenAI

# استيراد ملفاتك الخاصة بالبيانات والتعليمات
from static_replies import static_prompt, replies
from services_data import services

# حمل المتغيرات البيئية من ملف .env لو بتجرب محليًا
load_dotenv()

# تعريف المتغيرات البيئية
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = "https://openai.chatgpt4mena.com/v1"

ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN") # إضافة متغير الـ Client-Token

app = Flask(__name__)
session_memory = {}

# تهيئة عميل OpenAI
client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_API_BASE
)

# دالة بناء الـ prompt للأسعار باستخدام بيانات ملف services_data.py
def build_price_prompt():
    lines = []
    for item in services:
        # مؤقتًا نطبع كل خدمة كسطر أو نبنيها
        line = f"- {item['platform']} | {item['type']} | {item['count']} = {item['price']} جنيه ({item['audience']})"
        lines.append(line)
    return "\n".join(lines)

@app.route("/webhook", methods=["POST"])
# دالة التواصل مع ChatGPT
def ask_chatgpt(message, sender_id):
    # لو مفيش ذاكرة للمحادثة دي، بنبدأها بـ system prompt
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

    # إضافة رسالة المستخدم لذاكرة المحادثة
    session_memory[sender_id].append({"role": "user", "content": message})

    print("OPENAI_API_KEY being used:", OPENAI_API_KEY) # للمراجعة والتحقق

    try:
        # إرسال المحادثة لـ OpenAI API
        response = client.chat.completions.create(
            model="gpt-4", # تأكد أن هذا الموديل متاح في خدمتك
            messages=session_memory[sender_id],
            max_tokens=400 # عدد الكلمات الأقصى للرد
        )
        data = response.model_dump() # تحويل الرد لقاموس عشان نقدر نطبع كل حاجة
        print("🔁 GPT raw response:", data)

        # استخلاص الرد من OpenAI
        if "choices" in data and data["choices"] and "message" in data["choices"][0]:
            reply_text = data["choices"][0]["message"]["content"].strip()
            # إضافة رد البوت لذاكرة المحادثة
            session_memory[sender_id].append({"role": "assistant", "content": reply_text})
            return reply_text
        else:
            # لو الرد من OpenAI مش بالصيغة المتوقعة
            return "⚠ حصلت مشكلة في توليد الرد من الذكاء الاصطناعي. جرب تاني بعد شوية."
    except Exception as e:
        print("❌ Exception during OpenAI call:", e)
        return "⚠ في مشكلة تقنية حالياً مع الذكاء الاصطناعي. ابعتلي تاني بعد شوية."

# دالة إرسال الرسائل عبر ZAPI
def send_message(phone, message):
    # بناء الـ URL الخاص بـ ZAPI بناءً على المتغيرات البيئية الجديدة
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {
        "Content-Type": "application/json",
        "Client-Token": CLIENT_TOKEN # إضافة الـ Client-Token هنا
    }
    payload = {
        "phone": phone,
        "message": message
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        zapi_response_data = response.json()
        print("✅ تم إرسال الرد إلى العميل.")
        print("ZAPI Response:", zapi_response_data) # للمراجعة والتحقق
        return zapi_response_data
    except Exception as e:
        print("❌ Exception during ZAPI send_message:", e)
        return {"status": "error", "message": f"فشل إرسال الرسالة عبر ZAPI: {e}"}

# الـ Home route للتأكد إن البوت شغال
@app.route("/")
def home():
    return "✅ البوت شغال"

# الـ Webhook route لاستقبال رسائل واتساب
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    data = request.get_json(force=True)
    user_msg = data.get("message", "")
    sender_id = data.get("from", "")
    if request.method == "GET":
        return "✅ Webhook جاهز", 200

    if not user_msg or not sender_id:
        return jsonify({"error": "Invalid payload"}), 400
    print("✅ Webhook تم استدعاؤه")
    data = request.json # البيانات المستلمة من ZAPI
    print("📦 البيانات المستلمة:")
    print(data)

    if sender_id not in session_memory:
        session_memory[sender_id] = []

    session_memory[sender_id].append({"role": "user", "content": user_msg})

    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": static_prompt.format(
                prices=build_price_prompt(),
                confirm_text=replies["تأكيد_الطلب"]
            )},
            *session_memory[sender_id]
        ]
    )
    incoming_msg = None
    sender = None

    # محاولة استخلاص الرسالة ورقم المرسل من الـ JSON payload
    # بناءً على الصيغة الشائعة لـ ZAPI
    if data and "text" in data and "message" in data["text"]:
        incoming_msg = data["text"]["message"]
    elif data and "body" in data: # لو جاي من Twilio مباشرةً أو صيغة مختلفة
        incoming_msg = data["body"]

    if data and "phone" in data:
        sender = data["phone"]
    elif data and "From" in data: # لو جاي من Twilio مباشرةً
        sender = data["From"]

    if incoming_msg and sender:
        print(f"📩 رسالة من: {sender} - {incoming_msg}")

    reply = response.choices[0].message.content
    session_memory[sender_id].append({"role": "assistant", "content": reply})
        # استدعاء دالة الذكاء الاصطناعي للحصول على الرد
reply = ask_chatgpt(incoming_msg, sender)
requests.post(
    f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text",
    json={"to": sender_id, "message": reply}
)
        # منطق التحويل لموظف بشري
        if "تحويل لموظف" in reply:
            send_message(sender, "عزيزي العميل، لم أتمكن من فهم طلبك بشكل كامل أو أن طلبك يحتاج لتدخل بشري. سيتم تحويل محادثتك الآن إلى أحد ممثلي خدمة العملاء وسيتواصل معك في أقرب وقت ممكن.")
            # هنا ممكن تضيف كود لإرسال إشعار للموظف البشري (مثلاً عبر HTTP request لنظام CRM أو Slack)
            print(f"⚠ تم تحويل المحادثة لـ {sender} إلى موظف بشري.")
        else:
            # إرسال الرد العادي للعميل
            send_message(sender, reply)
    else:
        print("⚠ البيانات غير مكتملة أو غير متوقعة. Received data:", data)

    return jsonify({"status": "sent"}), 200Add commentMore actions
    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
