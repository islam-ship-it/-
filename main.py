import os
import json
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from services_data import services
from session_storage import session_memory
from static_replies import static_prompt  # ✅ دي الصح
from session_storage import get_session, save_session

app = Flask(__name__)

# متغيرات البيئة
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_MODEL = os.getenv("OPENAI_API_MODEL", "gpt-3.5-turbo")

client = OpenAI(api_key=OPENAI_API_KEY)

def ask_chatgpt(message, sender_id):
    messages = session_memory.get(sender_id, [])
    if not messages:
        # أول رسالة من العميل، حط البرومبت الأساسي
        messages.append({"role": "system", "content": static_prompt(services)})
    messages.append({"role": "user", "content": message})

    chat = client.chat.completions.create(
        model=OPENAI_API_MODEL,
        messages=messages
    )

    reply = chat.choices[0].message.content
    session_memory[sender_id] = messages + [{"role": "assistant", "content": reply}]
    return reply

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    
    # جلب الرسالة
    incoming_msg = None
    sender = None
    if data.get("text") and data.get("message"):
        incoming_msg = data["message"]["text"]
    elif "body" in data:
        incoming_msg = data["body"]

    if "phone" in data:
        sender = data["phone"]
    elif "From" in data:
        sender = data["From"]

    if incoming_msg and sender:
        reply = ask_chatgpt(incoming_msg, sender)
        # إرسال الرد
        requests.post(
            f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text",
            json={"to": sender, "message": reply}
        )
        return jsonify({"status": "sent"}), 200
    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

