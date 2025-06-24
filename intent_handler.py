import re

def detect_intent(message, session, message_type=None):
    message = str(message).strip().lower()

    # صورة دفع والمرحلة انتظار الدفع
    if message_type == "image" and session.get("stage") == "waiting_payment":
        return "confirm_payment"

    # رسالة دفع نصية
    if message_type == "payment_text":
        return "confirm_payment"

    # رابط خدمة
    if message_type == "link":
        return "send_link"

    # تحليل رقم المتابعين أو الكمية داخل الرسالة (لو موجود)
    count_match = re.search(r"\d{2,6}", message)
    if count_match:
        session["detected_count"] = int(count_match.group())

    # سؤال عن سعر أو خدمة
    if any(word in message for word in [
        "سعر", "كام", "بكام", "تكلفة", "ثمن",
        "عايز", "عاوز", "متابعين", "لايكات", "مشاهدات",
        "فولو", "لايك", "مشتركين", "تفاعل", "انستا", "فيس", "تيك", "يوتيوب", "سناب", "لينكد", "واتس", "كواي"
    ]):
        return "ask_price"

    # تأكيد الطلب
    if any(word in message for word in [
        "تمام", "موافق", "ماشي", "اوكي", "اوكى", "اوكيه", "كمل", "يلا", "اكمل"
    ]):
        return "confirm_order"

    # مدة التنفيذ
    if any(word in message for word in [
        "المدة", "قد إيه", "هيخلص", "بياخد وقت", "وقت التنفيذ", "هينفذ", "بتاخد وقت"
    ]):
        return "ask_duration"

    # أي رسالة تانية
    return "followup"
