import os
import logging
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# إعداد اللوجينج
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("main")

# إعدادات البوت
BOT_TOKEN = os.getenv("BOT_TOKEN", "8006378063:AAFlHqpGmfIU6rnI1s7MO7Wde9ikUJXMXXI")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# دالة إرسال الرد
def send_business_reply(business_connection_id, message_id, reply_text):
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "business_connection_id": business_connection_id,
        "message_thread_id": message_id,
        "text": reply_text,
        "reply_parameters": {
            "message_id": message_id
        }
    }

    logger.debug("📤 إرسال رد عبر sendMessage:")
    logger.debug(f"📡 URL: {url}")
    logger.debug(f"📦 Payload: {payload}")

    try:
        response = requests.post(url, json=payload)
        logger.info("📤 تم إرسال الرد. %s - %s", response.status_code, response.text)

        if response.status_code != 200:
            logger.error("❌ فشل إرسال الرسالة: %s", response.text)

        return response
    except Exception as e:
        logger.exception("🔥 حصل استثناء أثناء إرسال الرسالة:")
        return None

# Webhook endpoint
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    logger.debug("📩 تحديث Webhook:")
    logger.debug(data)

    if "business_message" in data:
        message = data["business_message"]
        user_id = message["from"]["id"]
        business_connection_id = message["business_connection_id"]
        message_id = message["message_id"]
        text = message.get("text", "")

        logger.info(f"📥 رسالة من {user_id} - Business: {business_connection_id} - النص: {text}")

        # رد تلقائي جاهز
        reply_text = (
            "أهلًا يا فندم 😊\n"
            "لو حضرتك تريد تزود متابعين، لايكات، مشاهدات، أو تعليقات على فيسبوك، تيك توك، أو إنستجرام،\n"
            "قول لي:\n"
            "🔹 نوع الخدمة\n"
            "🔹 الجمهور (مصريين فقط أو مصريين وعرب)\n"
            "🔹 العدد المطلوب\n"
            "وأنا هجهزلك العرض المناسب ⚡"
        )

        send_business_reply(business_connection_id, message_id, reply_text)

    return jsonify({"status": "ok"})

# Health check
@app.route("/", methods=["GET"])
def index():
    return "✅ Bot is running."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


