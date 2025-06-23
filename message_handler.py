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

def normalize_numbers(text):
    # يحول الأرقام العربية إلى إنجليزية
    arabic_nums = '٠١٢٣٤٥٦٧٨٩'
    western_nums = '0123456789'
    translation_table = str.maketrans(arabic_nums, western_nums)
    return text.translate(translation_table)

def match_service(text):
    text = normalize_numbers(text.lower())
    for s in services:
        platform = s["platform"].lower()
        count = str(s["count"])
        if platform in text and count in text:
            return s
    return None

def handle_message(text, sender_id, message_type="text"):
    session = get_session(sender_id)
    status = session["status"]

    if detect_image(message_type) and status == "waiting_payment":
        session["status"] = "completed"
        save_session(sender_id, {
            "history": session["history"],
            "status": session["status"]
        })
        return replies["تأكيد_التحويل"]

    if detect_image(message_type):
        return replies["صورة_غير_مفهومة"]

    if status == "idle":
        service = match_service(text)
        if service:
            session["status"] = "waiting_link"
            session["history"].append({"role": "user", "content": text})
            save_session(sender_id, session)
            return replies["طلب_الرابط"].format(price=service["price"])

    if detect_link(text) and status == "waiting_link":
        session["status"] = "waiting_payment"
        session["history"].append({"role": "user", "content": text})
        save_session(sender_id, session)
        return replies["طلب_الدفع"]

    if detect_payment(text) and status == "waiting_payment":
        session["status"] = "completed"
        session["history"].append({"role": "user", "content": text})
        save_session(sender_id, session)
        return replies["تأكيد_التحويل"]

    if status == "completed":
        service = match_service(text)
        if service:
            session["status"] = "waiting_link"
            session["history"] = [{"role": "user", "content": text}]
            save_session(sender_id, session)
            return replies["طلب_الرابط"].format(price=service["price"])

    session["history"].append({"role": "user", "content": text})
    save_session(sender_id, session)
    return None
