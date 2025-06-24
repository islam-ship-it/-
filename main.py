import os
from fastapi import FastAPI, Request
from pydantic import BaseModel
import uvicorn
import openai
from static_replies_ready import replies, static_prompt
from services_data_ready import services
from session_storage_ready import get_session, save_session
from intent_handler_ready import analyze_intent
from rules_engine_ready import apply_bot_rules
from bot_control_ready import is_bot_active
from link_validator_ready import validate_service_link

app = FastAPI()

openai.api_key = os.getenv("OPENAI_API_KEY", "your-api-key")

class Message(BaseModel):
    sender_id: str
    message: str
    media_type: str = None

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    msg = Message(**data)

    # التحقق إذا كان البوت مفعل لهذا المستخدم
    if not is_bot_active(msg.sender_id):
        return {"response": "❌ تم إيقاف البوت مؤقتًا لهذا العميل."}

    # تحميل الجلسة
    session = get_session(msg.sender_id)

    # تحديد النية
    intent_data = analyze_intent(msg.message, msg.media_type, session, services)

    # تحديث الجلسة
    session.update(intent_data)
    save_session(msg.sender_id, session)

    # تطبيق القواعد الذكية
    response = apply_bot_rules(msg, session, services, replies, static_prompt)

    return {"response": response}

if _name_ == "_main_":
    uvicorn.run(app, host="0.0.0.0", port=8000)
