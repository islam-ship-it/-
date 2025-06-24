# model_selector.py

def choose_model(message, matched_services):
    message_length = len(message)

    # لو مفيش خدمة واضحة، نستخدم نموذج خفيف
    if not matched_services:
        return "GPT-4.1 NANO", "Model: خفيف لأنه مفيش خدمة واضحة"

    # لو رسالة قصيرة وواضحة جدًا → استخدام نموذج اقتصادي
    if message_length < 200 and len(matched_services) <= 1:
        return "GPT-4.1 NANO", "Model: خفيف لأن الطلب بسيط"

    # لو فيه أكتر من خدمة أو الرسالة طويلة → نموذج أقوى
    if message_length > 500 or len(matched_services) > 2:
        return "gpt-4o", "Model: قوي لأن الطلب طويل أو فيه خدمات كتير"

    # الافتراضي: نموذج متوسط
    return "GPT-4.1 NANO", "Model: افتراضي"
