import os
import time
import re
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from session_storage import get_session, save_session

# إعدادات البيئة
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
ASSISTANT_ID = "asst_NZp1j8UmvcIXqk5GCQ4Qs52s"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

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

def deep_local_clean(text):
    # فلترة عميقة لأي مرجع أو أكواد أو رموز تقنية
    text = re.sub(r"\[.?\.txt.?\]", "", text)  # ملفات نصية
    text = re.sub(r"\[.?prices.?\]", "", text)  # ملف أسعار
    text = re.sub(r"\[.?info.?\]", "", text)    # ملف معلومات
    text = re.sub(r"\[.?scenarios.?\]", "", text)  # ملف سيناريوهات
    text = re.sub(r"\[.?†.?\]", "", text)  # مرجع من نوع †
    text = re.sub(r"\[.*?\]", "", text)  # أي قوسين مربعين
    text = re.sub(r"\s+", " ", text)  # مسافات زيادة
    return text.strip()

def clean_reply_with_mistral(text):
    url = f"{OPENROUTER_API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "mistral/mistral-7b-instruct",
        "messages": [
            {"role": "system", "content": "مهمتك تنضف الجملة دي من أي رموز، أسماء ملفات، أكواد تقنية أو كلام إنجليزي زائد، وتعيد صياغتها جاهزة للعميل باللهجة المصرية:"},
            {"role": "user", "content": text}
        ]
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("❌ Mistral Cleaning Error:", e)
        return text

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
    latest_reply = None
    for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            latest_reply = msg.content[0].text.value.strip()
            break
    if latest_reply:
        cleaned = deep_local_clean(latest_reply)
        return clean_reply_with_mistral(cleaned)
    return "⚠ في مشكلة مؤقتة، حاول تاني."

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

