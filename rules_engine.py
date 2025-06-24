def get_next_action(session, message):
    status = session.get("status", "idle")

    if status == "waiting_link":
        session["status"] = "waiting_payment"
        return "تم استلام الرابط. يرجى تحويل المبلغ الآن لإتمام الطلب."

    if status == "waiting_payment":
        return "نحن في انتظار التحويل لإكمال الطلب."

    return None


def apply_rules(message, intent, session, services, replies):
    # نستخدم الدالة الذكية لتحديد الخطوة التالية من السياق
    response = get_next_action(session, message)

    # لو في رد من get_next_action نرجعه
    if response:
        return response

    # لو مفيش، نستخدم رد افتراضي من ChatGPT أو غيره
    return replies.get("رد_افتراضي", "هل يمكنك توضيح طلبك؟")
