import os
import json
from flask import Flask, request
from static_replies import replies, static_prompt
from services_data import services
from session_storage import get_session, save_session
from bot_control import is_bot_active
from intent_handler import analyze_intent
from rules_engine import evaluate_rules
from link_validator import validate_service_link

app = Flask(__name__)

@app.route('/', methods=['POST'])
def webhook():
    data = request.json
    sender_id = data.get("sender_id")
    message = data.get("message")
    message_type = data.get("message_type")
    session = get_session(sender_id)

    if not is_bot_active(sender_id):
        return json.dumps({"response": None})

    # ذكاء البوت: فهم النية وتحديد المرحلة الحالية
    intent_result = analyze_intent(message, message_type, session, services)
    updated_session, reply = evaluate_rules(message, message_type, session, intent_result, services, replies)

    save_session(sender_id, updated_session)
    return json.dumps({"response": reply})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
