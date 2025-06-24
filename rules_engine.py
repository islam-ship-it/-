def get_next_action(session, message):
    """
    تحديد الخطوة التالية في السيناريو بناءً على حالة الجلسة الحالية
    """
    status = session.get("status", "idle")

    if status == "waiting_link":
        session["status"] = "waiting_payment"
        return "✅ تم استلام الرابط بنجاح.\nيرجى الآن تحويل المبلغ لإتمام الطلب."

    if status == "waiting_payment":
        return "📌 نحن في انتظار التحويل لإكمال تنفيذ طلبك."

    return None


def apply_rules(message, intent, session, services, replies):
    """
    تطبيق منطق القواعد الذكية على الرسالة الواردة لتحديد الرد المناسب
    """
    # محاولة تحديد الرد من خلال السياق (مثل: بعت الرابط أو منتظر دفع)
    contextual_response = get_next_action(session, message)
    if contextual_response:
        return contextual_response

    # لو مفيش حالة سياقية، يتم الرد الافتراضي
    return replies.get("رد_افتراضي", "❓ من فضلك وضّح طلبك بشكل أوضح.")
