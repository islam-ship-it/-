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
    text = text.lower()

    synonyms = {
        "facebook": ["فيس", "فيسبوك", "fb"],
        "instagram": ["انستا", "انستجرام", "انستغرام"],
        "tiktok": ["تيك", "تيك توك", "tiktok"],
        "youtube": ["يوتيوب", "يوتوب", "yt"],
        "followers": ["متابع", "متابعين"],
        "likes": ["لايك", "لايكات", "اعجاب", "اعجابات"],
        "views": ["مشاهدة", "مشاهدات"],
        "subscribers": ["مشتركين", "اشتراك"]
    }

    def matches(value, group):
        return any(alt in text for alt in synonyms.get(value.lower(), [value.lower()]))

    for s in services:
        platform_match = matches(s["platform"], synonyms)
        type_match = matches(s["type"], synonyms)
        count_match = str(s["count"]) in text or str(int(s["count"])) in text

        if platform_match and type_match and count_match:
            return s
    return None

def handle_message(text, sender_id, message_type="text"):
    session = get_session(sender_id)
    status = session["status"]

    # ✅ صورة دفع والعميل مستني يدفع
    if detect_image(message_type) and status == "waiting_payment":
        session["status"] = "completed"
        save_session(sender_id, session["history"], session["status"])
        return replies["تأكيد_التحويل"]

    # ❌ صورة عشوائية والعميل مش مستني دفع
    if detect_image(message_type):
        return replies["صورة_غير_مفهومة"]

    # 🟡 طلب خدمة جديدة
    if status == "idle":
        service = match_service(text)
        if service:
            session["status"] = "waiting_link"
            session["history"].append({"role": "user", "content": text})
            save_session(sender_id, session["history"], session["status"])
            return replies["طلب_الرابط"].format(price=service["price"])

    # 🔗 بعت رابط
    if detect_link(text) and status == "waiting_link":
        session["status"] = "waiting_payment"
        session["history"].append({"role": "user", "content": text})
        save_session(sender_id, session["history"], session["status"])
        return replies["طلب_الدفع"]

    # 💰 بعت تأكيد دفع
    if detect_payment(text) and status == "waiting_payment":
        session["status"] = "completed"
        session["history"].append({"role": "user", "content": text})
        save_session(sender_id, session["history"], session["status"])
        return replies["تأكيد_التحويل"]

    # 🔁 بدأ طلب جديد بعد ما خلص
    if status == "completed":
        service = match_service(text)
        if service:
            session["status"] = "waiting_link"
            session["history"] = []
            session["history"].append({"role": "user", "content": text})
            save_session(sender_id, session["history"], session["status"])
            return replies["طلب_الرابط"].format(price=service["price"])

    # 🤖 أي حاجة تانية: شغّل GPT
    session["history"].append({"role": "user", "content": text})
    save_session(sender_id, session["history"], session["status"])
    return None

