import os
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from session_storage import get_session, save_session

# إعدادات OpenAI الرسمية
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
    session["history"].append({"role": "user", "content": message})

    try:
        response = client.chat.completions.create(
            model="ft:gpt-3.5-turbo-1106:boooot-waaaatsaaap::BmH1xi0x:ckpt-step-161",
            messages=session["history"][-10:],
            max_tokens=500
        )
        reply_text = response.choices[0].message.content.strip()
        session["history"].append({"role": "assistant", "content": reply_text})
        save_session(sender_id, session)
        return reply_text
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
    media_type = data.get("type", "text")

    if not sender:
        return jsonify({"status": "no sender"}), 400

    # الرد من النموذج المدرب مباشرة
    response = ask_chatgpt(msg, sender)
    send_message(sender, response)

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
