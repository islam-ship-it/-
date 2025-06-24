from intent_handler import detect_intent

def get_next_action(session, message):
    intent = detect_intent(message)

    if session.get("completed"):
        return "تم_الانتهاء"

    if intent == "طلب_خدمة":
        return "عرض_السعر"

    if intent == "تأكيد_طلب" and session.get("last_step") == "عرض_السعر":
        return "طلب_رابط"

    if intent == "رابط_الخدمة" and session.get("last_step") in ["عرض_السعر", "طلب_رابط"]:
        return "طلب_دفع"

    if intent == "تأكيد_دفع" and session.get("last_step") == "طلب_دفع":
        return "تأكيد_نهائي"

    if intent == "استفسار_عن_الضمان":
        return "رد_على_الضمان"

    return "غير_معروف"
