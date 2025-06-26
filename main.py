import os
import time
import json
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from session_storage import get_session, save_session

# إعدادات البيئة
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = "https://api.openai.com/v1"
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

app = Flask(__name__)

# عميل GPT الأساسي والمراجع (نفس الـ OpenAI)
client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)
review_client = client  # لأننا هنستخدم نفس الـ OpenAI في المراجعة

# مراجعة الرد باستخدام GPT-4o
def review_reply_with_gpt4o(text):
    try:
        review_prompt = (
            "راجع الرد التالي باللهجة المصرية من حيث التنسيق والأسلوب، من غير ما تغيّر أي معلومة أو سعر أو لهجة أو تفاصيل. "
            "لو فيه حاجة مش واضحة وضّحها، ولو فيه خطوة ممكن العميل يعملها بعد كده، وضّحها بأسلوب احترافي مقنع. "
            "خلّي الرد في النهاية فيه CTA ذكي يحفّز العميل ياخد الخطوة الجاية."
        )
        response = review_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": review_prompt},
                {"role": "user", "content": text}
            ],
            max_tokens=500
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print("❌ Review Error:", e)
        return text  # fallback

# الرد على الرسالة
def ask_chatgpt(message, sender_id):
    session = get_session(sender_id)

    if not any(msg["role"] == "system" for msg in session["history"]):
        session["history"].insert(0, {
            "role": "system",
            "content": (
                "أنت مساعد ذكي ومحترف، هدفك الأساسي ترد على عملاء “متجر المتابعين” باللهجة المصرية بأسلوب واضح ومقنع. "
                "هدفك التاني إنك تساعد العميل على اتخاذ قرار الشراء وتقفل الصفقة بسرعة وباحتراف.\n\n"
                "افهم المرحلة اللي العميل فيها كويس:\n"
                "- لو بيسأل عن خدمة، اشرح له التفاصيل ببساطة والأسعار بدقة.\n"
                "- لو بعت رابط، راجعه سريعًا واطلب تحويل الكاش .\n"
                "- لو دفع أو بعت كاش، راجع البيانات وبلّغه بالخطوة الجاية.\n"
                "- لو محتار، اقترح له أقرب عرض مناسب يشجعه يبدأ.\n\n"
                "اتكلم بثقة، خليك سريع ومباشر، ودايمًا اختم كل رسالة بسؤال ذكي (CTA) يحرّك العميل للخطوة التالية، زي:\n"
                "- تحب أجهزلك الخدمة ونبدأ؟\n"
                "- أشرحلك طريقة الدفع؟\n"
                "- خلينا نبدأ بأول طلب نجرب بيه؟\n\n"
                "تجنّب المجاملات أو العبارات الإنشائية الكتير، وخليك دايمًا مركز على إقناع العميل وتحويله من مرحلة التعارف لاستفسار لاعتراض لإقناع لطلب لتقفيل التعامل وتاخد منه الكاش وتفاصيل الطلب."
            )
        })

    session["history"].append({"role": "user", "content": message})

    try:
        response = client.chat.completions.create(
            model="ft:gpt-4.1-2025-04-14:boooot-waaaatsaaap:bot-shark:Bmcj13tH",
            messages=session["history"][-10:],
            max_tokens=500
        )
        raw_reply = response.choices[0].message.content.strip()
        print("🤖 الرد الأصلي:", raw_reply)

        # مراجعة الرد باستخدام GPT-4o
        final_reply = review_reply_with_gpt4o(raw_reply)

        session["history"].append({"role": "assistant", "content": final_reply})
        save_session(sender_id, session)

        return final_reply
    except Exception as e:
        print("❌ GPT Error:", e)
        return "⚠ في مشكلة تقنية مؤقتة. جرب تبعت تاني كمان شوية."

# إرسال الرسالة عبر ZAPI
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

# Webhook
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

    if not sender:
        return jsonify({"status": "no sender"}), 400

    reply = ask_chatgpt(msg, sender)
    send_message(sender, reply)
    return jsonify({"status": "sent"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
