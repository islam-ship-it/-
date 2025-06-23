import re
from static_replies import replies
from session_storage import get_session, save_session
from services_data import services

def detect_link(text):
    return "http" in text or "www." in text or any(domain in text for domain in [
        "tiktok.com", "facebook.com", "instagram.com", "youtube.com"
    ])

def detect_payment(text):
    keywords = ["تم", "حولت", "الفلوس", "الدفع", "وصل", "سكرين", "صورة", "دفعت"]
    return any(word in text.lower() for word in keywords)

def detect_image(message_type):
    return message_type == "image"

def match_service(text):
    for s in services:
        if s["platform"].lower() in text.lower() and str(s["count"]) in text:
            return s
    return None

def handle_message(text, sender_id, message_type="text"):
    session = get_session(sender_id)
    history = session["history"]
    status = session["status"]

    # ✅ صورة تحويل حقيقية أثناء انتظار الدفع
    if detect_image(message_type) and status == "waiting_payment":
        save_session(sender_id, history, "completed")
        return replies["تأكيد_التحويل"]

    # ❌ صورة مش وقت دفع أو مش مفهومة
    if detect_image(message_type):
        return replies["صورة_غير_مفهومة"]

    # 🟢 بداية جديدة - استفسار عن خدمة
    if status == "idle":
        service = match_service(text)
        if service:
            history.append({"role": "user", "content": text})
            save_session(sender_id, history, "waiting_link")
            return replies["طلب_الرابط"].format(price=service["price"])

    # 🔗 العميل بعت رابط
    if detect_link(text) and status == "waiting_link":
        history.append({"role": "user", "content": text})
        save_session(sender_id, history, "waiting_payment")
        return replies["طلب_الدفع"]

    # 💵 العميل بيأكد الدفع بالكلام
    if detect_payment(text) and status == "waiting_payment":
        history.append({"role": "user", "content": text})
        save_session(sender_id, history, "completed")
        return replies["تأكيد_التحويل"]

    # 🔁 طلب جديد بعد ما خلص الطلب السابق
    if status == "completed":
        service = match_service(text)
        if service:
            history = [{"role": "user", "content": text}]
            save_session(sender_id, history, "waiting_link")
            return replies["طلب_الرابط"].format(price=service["price"])

    # 👇 أي حاجة تانية تبعت لـ GPT
    history.append({"role": "user", "content": text})
    save_session(sender_id, history, status)
    return None
