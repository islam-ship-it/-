import time
from openai import OpenAI
from config import OPENAI_API_KEY, ASSISTANT_ID, OPENROUTER_API_BASE, OPENROUTER_API_KEY
import requests
from database import get_session, save_session

client = OpenAI(api_key=OPENAI_API_KEY)

def organize_reply(text):
    url = f"{OPENROUTER_API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "mistral/mistral-7b-instruct",
        "messages": [
            {"role": "system", "content": "نظم الرد بشكل احترافي مع رموز ✅ 🔹 💳."},
            {"role": "user", "content": text}
        ]
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"❌ خطأ تنظيم الرد: {e}")
        return text

def ask_assistant(message, sender_id, name=""):
    session = get_session(sender_id)
    if name and not session.get("name"):
        session["name"] = name
    if not session.get("thread_id"):
        thread = client.beta.threads.create()
        session["thread_id"] = thread.id

    session["message_count"] += 1
    session["history"].append({"role": "user", "content": message})
    session["history"] = session["history"][-10:]
    save_session(sender_id, session)

    intro = f"عميل اسمه: {session['name'] or 'غير معروف'}, رقمه: {sender_id}."
    full_message = f"{intro}\n{message}"

    client.beta.threads.messages.create(thread_id=session["thread_id"], role="user", content=full_message)
    run = client.beta.threads.runs.create(thread_id=session["thread_id"], assistant_id=ASSISTANT_ID)

    while True:
        run_status = client.beta.threads.runs.retrieve(thread_id=session["thread_id"], run_id=run.id)
        if run_status.status == "completed":
            break
        time.sleep(2)

    messages = client.beta.threads.messages.list(thread_id=session["thread_id"])
    for msg in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
        if msg.role == "assistant":
            reply = msg.content[0].text.value.strip()
            return organize_reply(reply)
    return "⚠ مشكلة مؤقتة، حاول تاني."
