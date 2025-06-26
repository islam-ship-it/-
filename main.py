import os
import time
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
client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE)

def ask_chatgpt(message, sender_id):
    session = get_session(sender_id)

    # system prompt مرة واحدة
    if not any(msg["role"] == "system" for msg in session["history"]):
        system_prompt = {
            "role": "system",
            "content": "أنت مساعد ذكي ومحترف بتتكلم باللهجة المصرية، شغلك هو إنك ترد على استفسارات عملاء “متجر المتابعين” بكل وضوح وبشكل تفصيلي وتركز في الأسئلة الخاصة بالخدمات والأسعار وتسال سؤال تكميلي في اخر كل رساله عشان تدخل العميل في المرحله الي بعدها وتقفل معاه الديل ."
        }
        session["history"].insert(0, system_prompt)

    # أضف رسالة المستخدم
    session["history"].append({"role": "user", "content": message})

    try:
        # الرد الأول من النموذج المتدرب
        response = client.chat.completions.create(
            model="ft:gpt-4.1-2025-04-14:boooot-waaaatsaaap:bot-shark:Bmcj13tH",
            messages=session["history"][-10:],
            max_tokens=500
        )
        raw_reply = response.choices[0].message.content.strip()

        # ✅ المراجعة الذاتية: خليه يراجع نفسه
        review_response = client.chat.completions.create(
            model="ft:gpt-4.1-2025-04-14:boooot-waaaatsaaap:bot-shark:Bmcj13tH",
            messages=[
                {"role": "system", "content": "راجع الرد التالي من حيث الأسلوب والتنسيق باللهجة المصرية، وعدّله لو محتاج تعديل بسيط. خليه يدي انطباع احترافي ويسأل سؤال تاني تكميلي زكي جدا يحول العميل ل المرحله الي بعدها."},
                {"role": "user", "content": raw_reply}
            ],
            max_tokens=500
        )
        final_reply = review_response.choices[0].message.content.strip()

        # سجل الرد النهائي
        session["history"].append({"role": "assistant", "content": final_reply})
        save_session(sender_id, session)

        return final_reply

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

    if not sender:
        return jsonify({"status": "no sender"}), 400

    reply = ask_chatgpt(msg, sender)
    reply = reply.replace("[رقم_الكاش]", "01015654194")  # 🟢 استبدال رقم الكاش
    send_message(sender, reply)
    return jsonify({"status": "sent"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
