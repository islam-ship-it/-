# intent_handler.py

def detect_intent(message, session, message_type=None):
    """
    تحليل نية المستخدم بناءً على الرسالة ونوعها والسياق السابق في الجلسة
    """
    message = str(message).strip().lower()

    # لو دي صورة دفع والمرحلة انتظار الدفع
    if message_type == "image" and session.get("stage") == "waiting_payment":
        return "confirm_payment"

    # لو قال دفع أو حولت
    if message_type == "payment_text":
        return "confirm_payment"

    # لو بعت رابط
    if message_type == "link":
        return "send_link"

    # لو بيسأل على سعر أو خدمة
    if any(word in message for word in [
        "سعر", "كام", "بكام", "تكلفة", "ثمن",
        "عايز", "عاوز", "متابعين", "لايكات", "مشاهدات",
        "فولو", "لايك", "مشتركين", "تفاعل", "انستا", "فيس", "تيك", "يوتيوب", "سناب", "لينكد", "واتس", "كواي"
    ]):
        return "ask_price"

    # لو العميل وافق
    if any(word in message for word in [
        "تمام", "موافق", "ماشي", "اوكي", "اوكى", "اوكيه", "كمل", "يلا", "اكمل"
    ]):
        return "confirm_order"

    # لو بيسأل على مدة التنفيذ
    if any(word in message for word in [
        "المدة", "قد إيه", "هيخلص", "بياخد وقت", "وقت التنفيذ", "هينفذ", "بتاخد وقت"
    ]):
        return "ask_duration"

    # أي حاجة تانية تبقى متابعة عامة
    return "followup"
