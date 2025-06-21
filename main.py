from flask import Flask, request, jsonify
import requests
import os
from static_replies import static_prompt
from services_data import services

app = Flask(__name__)
session_memory = {}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")

def send_message(to, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    payload = {"to": to, "message": message}
    headers = {"Content-Type": "application/json"}
    return requests.post(url, json=payload, headers=headers)

def build_price_prompt():
    lines = []
    for item in services:
        line = f"- {item['platform']} | {item['type']} | {item['count']} = {item['price']} جنيه ({item['audience']})"
        lines.append(line)
    return "\n".join(lines)

def ask_chatgpt(message, sender_id):
    if sender_id not in session_memory:
        session_memory[sender_id] = [
            {
                "role": "system",
                "content": static_prompt.format(
                    prices=build_price_prompt(),
                    confirm_text="لتأكيد الطلب اكتب (أكدلي الطلب)"
                )
            }
        ]

    session_memory[sender_id].append({"role": "user", "content": message})

    try:
        response = requests.post(
            "https://openai.chatgpt4mena.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o",
                "messages": session_memory[sender_id],
                "temperature": 0.5
            }
        )

        data = response.json()
        print("🔁 GPT raw response:", data)

        if "choices" in data and data["choices"] and "message" in data["choices"][0]:
            reply_text = data["choices"][0]["message"]["content"].strip()
            session_memory[sender_id].append({"role": "assistant", "content": reply_text})
            return reply_text
        else:
            return "معرفتش أرد دلوقتي 😓"
    except Exception as e:
        print("[ERROR from GPT]", str(e))
        return "حصلت مشكلة مؤقتة، جرب تبعت تاني 🙏"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    incoming_msg = None
    sender_id = None

    if "text" in data.get("message", {}):
        incoming_msg = data["message"]["text"]
    elif "body" in data:
        incoming_msg = data["body"]

    if "phone" in data:
        sender_id = data["phone"]
    elif "From" in data:
        sender_id = data["From"]

    if incoming_msg and sender_id:
        print(f"رسالة من {sender_id}: {incoming_msg}")
        reply = ask_chatgpt(incoming_msg, sender_id)

        requests.post(
            f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text",
            json={"to": sender_id, "message": reply}
        )

        if "تحويل لوظيفة" in reply:
            send_message(sender_id, reply)
        else:
            send_message(sender_id, reply)

        return jsonify({"status": "sent"}), 200

    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
