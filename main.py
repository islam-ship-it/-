import os
import time
import re
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from session_storage import get_session, save_session

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
ASSISTANT_ID = "asst_NZp1j8UmvcIXqk5GCQ4Qs52s"

app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        return response.json()
    except Exception as e:
        print("❌ ZAPI Error:", e)
        return {"status": "error", "message": str(e)}

def ask_assistant(message, sender_id):
    session = get_session(sender_id)
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id
        save_session(sender_id, session)
    thread_id = session["thread_id"]
    client.beta.threads.messages.create(thread_id=thread_id, role="user", content=message)
    run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=ASSISTANT_ID)
    while True:
        run_status = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run_status.status == "completed":
            break
        time.sleep(2)
    messages = client.beta.threads.messages.list(thread_id=thread_id)
    latest_reply_parts = []
    for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            print("📦 الرد بالكامل اللي راجع من المساعد:")
            print(msg)  # طباعة الرسالة كاملة لتحليلها
            for part in msg.content:
                if part.type == "text" and part.text.value:
                    clean_part = re.sub(r"\[.?\.txt.?\]", "", part.text.value).strip()
                    latest_reply_parts.append(clean_part)
            break
    final_reply = "\n".join(latest_reply_parts).strip()
    return final_reply if final_reply else "⚠ في مشكلة مؤقتة، حاول تاني."

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "✅ Webhook شغال", 200
    data = request.json
    msg = data.get("text", {}).get("message") or data.get("body", "")
    sender = data.get("phone") or data.get("From")
    if not sender:
        return jsonify({"status": "no sender"}), 400
    reply = ask_assistant(msg, sender)
    send_message(sender, reply)
    return jsonify({"status": "sent"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

