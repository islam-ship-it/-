from flask import Flask, request, jsonify
import requests
import os
from static
_
replies import static
_prompt
from services
_
data import services
app = Flask(__
name
)__
OPENAI
ZAPI
ZAPI
ZAPI
API
_
_
KEY = os.getenv('OPENAI_API_KEY')
API
_
_
KEY")
INSTANCE
_
_
ID = os.getenv("ZAPI
INSTANCE
_
_
_
TOKEN = os.getenv("ZAPI
_
TOKEN")
BASE
_
_
URL = os.getenv("ZAPI
BASE
_
_
URL")
ID")
session
_
memory = {}
def send
_
whatsapp_
message(to
_
number, message):
url = f"
{ZAPI
BASE
_
_
URL}/instances/{ZAPI
INSTANCE
_
_
ID}/token/{ZAPI
_
TOKEN}/send-text"
payload = {
"to": to
_
number,
"message": message
}
headers = {"Content-Type": "application/json"}
response = requests.post(url, json=payload, headers=headers)
return response.json()
def call
_
chatgpt(session
_
id, user
_
message):
if session
id not in session
_
_
memory:
session
_
memory[session
_
id] = []
messages = session
_
memory[session
_
id]
messages.append({"role": "user"
"content": user
,
_
message})
response = requests.post(
"https://openai.chatgpt4mena.com/v1/chat/completions"
headers={
"Authorization": f"Bearer {OPENAI
API
_
_
KEY}"
,
"Content-Type": "application/json"
,
,
,}
json={
"model": "gpt-4o"
,
"messages": [{"role": "system"
static
_prompt(services)}] + messages,
"temperature": 0.5,
"content":
,
}
)
reply = response.json()["choices"][0]["message"]["content"]
messages.append({"role": "assistant"
,
"content": reply})
return reply
@app.route("/webhook"
, methods=["GET"
def webhook():
if request.method == "POST":
data = request.get
_json()
try:
,
"POST"])
msg = data["message"]
phone = msg["from"]
if
text = msg["text"]["body"]
print(f"[{phone}] {text}")
reply = call
_
chatgpt(phone, text)
print(f"[Bot Reply] {reply}")
send
_
whatsapp_
message(phone, reply)
except Exception as e:
print("[ERROR]"
, str(e))
return jsonify({"status": "ok"}), 200
return "OK"
, 200
name
== "
main
":
__
__
__
__
port = int(os.environ.get("PORT"
, 5000))
app.run(host=\'0.0.0.0\'
, port=port, debug=False)
