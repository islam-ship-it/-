# message_handler.py

from static_replies import replies
from services_data import services
from session_storage import get_session, save_session, reset_session

def is_payment_message(msg):
    keywords = ["حولت", "تم التحويل", "حولتلك", "حولتلِك", "بعته", "دفعت"]
    return any(kw in msg.lower() for kw in keywords)

def is_link(msg):
    return "http" in msg or "www." in msg

def build_price_prompt():
    return "\n".join([
        f"- {s['platform']} | {s['type']} | {s['count']} = {s['price']} جنيه ({s['audience']})"
        for s in services
    ])

def analyze_message(msg, sender, media_type=None):
    history = get_session(sender)

    # تحديد المرحلة من التاريخ
    previous_messages = [m["content"] for m in history if m["role"] == "user"]

    # 1. لو بعت صورة (سكرين شوت)
    if media_type == "image":
        if any("رابط" in m or "متابع" in m or "سعر" in m for m in previous_messages):
            reset_session(sender)
            return "✅ تم استلام صورة التحويل، الطلب قيد التنفيذ. شكرًا لثقتك ❤"
        else:
            return "📷 استلمت صورة، لو دي صورة التحويل ابعتلي الخدمة اللي طلبتها علشان نكمل ✨"

    # 2. لو كتب جملة فيها دفع
    if is_payment_message(msg):
        if any("رابط" in m or "سعر" in m for m in previous_messages):
            reset_session(sender)
            return "✅ تم تأكيد الدفع، الطلب هيبدأ تنفيذه خلال ساعات قليلة ✨"
        else:
            return "💬 تمام، بس لسه ما استلمناش تفاصيل الطلب أو الرابط، ابعتهم علشان نكمل."

    # 3. لو بعت رابط
    if is_link(msg):
        return replies["تأكيد_الطلب"]

    # 4. رسالة عادية، خليه يروح للـ ChatGPT
    return None
