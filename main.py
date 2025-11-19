import os
import json
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# -------------------------
# إعدادات
# -------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")     # مثال wf_xxxxx
WORKFLOW_VERSION = os.getenv("WORKFLOW_VERSION")  # مثال "version=6"
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

# تحقق من المتغيرات
required = {
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "WORKFLOW_ID": WORKFLOW_ID,
    "WORKFLOW_VERSION": WORKFLOW_VERSION,
    "MANYCHAT_API_KEY": MANYCHAT_API_KEY,
    "MANYCHAT_SECRET_KEY": MANYCHAT_SECRET_KEY
}

missing = [k for k,v in required.items() if not v]
if missing:
    raise SystemExit(f"❌ Error: Missing environment variables: {missing}")

# -------------------------
# إعداد Flask
# -------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# -------------------------
# استدعاء Workflow الجديد
# -------------------------
def call_workflow(user_message: str):
    url = "https://api.openai.com/v1/workflows/runs"

    payload = {
        "workflow_id": WORKFLOW_ID,
        "version": WORKFLOW_VERSION,       # "version=6"
        "input": {
            "user_message": user_message
        }
    }

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    logging.info("📡 Calling OpenAI Workflow...")

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code != 200:
        logging.error(f"❌ Workflow Error: {response.text}")
        return "⚠️ حصل خطأ أثناء تشغيل سير العمل."

    data = response.json()

    # استخراج نص الرد من workflow
    try:
        output_text = data["output"]["final_text"]
        return output_text
    except:
        return "⚠️ لم أستطع استخراج الرد من سير العمل."

# -------------------------
# إرسال الرد إلى ManyChat
# -------------------------
def send_manychat_reply(subscriber_id, text, platform="facebook"):
    url = "https://api.manychat.com/fb/sending/sendContent"

    channel = "instagram" if platform.lower() == "instagram" else "facebook"

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {
            "version": "v2",
            "content": {
                "messages": [
                    {"type": "text", "text": text}
                ]
            }
        },
        "channel": channel
    }

    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.post(url, headers=headers, json=payload)
        r.raise_for_status()
        logging.info(f"📨 Sent reply to {subscriber_id}")
    except Exception as e:
        logging.error(f"❌ ManyChat Error: {e}")

# -------------------------
# Webhook
# -------------------------
@app.route("/manychat_webhook", methods=["POST"])
def manychat_webhook():
    # تحقق من المفتاح
    auth = request.headers.get("Authorization")
    if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(force=True)
    contact = data.get("full_contact")

    if not contact:
        return jsonify({"error": "invalid"}), 400

    subscriber_id = contact.get("id")
    user_msg = contact.get("last_text_input") or contact.get("last_input_text")

    if not user_msg:
        return jsonify({"status": "no_input"}), 200

    logging.info(f"📩 Received: {user_msg}")

    ai_reply = call_workflow(user_msg)

    send_manychat_reply(subscriber_id, ai_reply)

    return jsonify({"status": "done"})

# -------------------------
# صفحة رئيسية
# -------------------------
@app.route("/")
def home():
    return "Workflow Bot Running"

# -------------------------
# تشغيل
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
