import re
from static_replies import replies
from session_storage import get_session, save_session
from services_data import services

def detect_link(text):
    return "http" in text or "www." in text or "tiktok.com" in text or "facebook.com" in text

def detect_payment(text):
    payment_keywords = ["تم التحويل", "حولت", "الفلوس", "وصل", "سكرين", "صورة"]
    return any(word in text.lower() for word in payment_keywords)

def detect_image(message_type):
    return message_type == "image"

def match_service(text):
    for s in services:
        if s["platform"].lower() in text.lower() and str(s["count"]) in text:
            return s
    return None

def handle_message(text, sender_id, message_type="text"):
    session = get_session(sender_id)
    status = session["status"]

    # ✅ صورة دفع والعميل مستني الدفع
    if detect_image(message_type) and status == "waiting_payment":
        session["status"] = "completed"
        save_session(sender_id, session)
        return replies["تأكيد_التحويل"]

    # ❌ صورة مش مفهومة (والعميل مش مستني دفع)
    if detect_image(message_type):
        return replies["صورة_غير_مفهومة"]

    # 🟡 العميل طلب خدمة جديدة
    if status == "idle":
        service = match_service(text)
        if service:
            session["status"] = "waiting_link"
            session["history"].append({"role": "user", "content": text})
            save_session(sender_id, session)
            return replies["طلب_الرابط"].format(price=service["price"])

    # 🟠 العميل بعت لينك للخدمة
    if detect_link(text) and status == "waiting_link":
        session["status"] = "waiting_payment"
        session["history"].append({"role": "user", "content": text})
        save_session(sender_id, session)
        return replies["طلب_الدفع"]

    # 🟢 العميل قال "حولت" أو حاجة شبه كده
    if detect_payment(text) and status == "waiting_payment":
        session["status"] = "completed"
        session["history"].append({"role": "user", "content": text})
        save_session(sender_id, session)
        return replies["تأكيد_التحويل"]

    # 🔁 العميل طلب خدمة جديدة بعد ما خلص واحدة
    if status == "completed":
        service = match_service(text)
        if service:
            session["status"] = "waiting_link"
            session["history"] = [{"role": "user", "content": text}]
            save_session(sender_id, session)
            return replies["طلب_الرابط"].format(price=service["price"])

    # 🔚 fallback → نرجع لـ GPT
    session["history"].append({"role": "user", "content": text})
    save_session(sender_id, session)
    return None
