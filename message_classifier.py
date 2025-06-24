import re

def classify_message_type(message):
    """
    تصنيف نوع الرسالة: صورة - رابط - نص عادي - تحويل
    """
    message = str(message).lower().strip()

    if message.startswith("http") or "facebook.com" in message or "instagram.com" in message or "tiktok.com" in message:
        return "link"

    elif any(word in message for word in ["حولت", "دفعت", "تم الدفع", "تم التحويل"]):
        return "payment_text"

    elif message == "صورة":
        return "image"

    else:
        return "text"
        
def validate_service_link(service_type, link):
    # لو مفيش لينك أصلاً
    if not link or not isinstance(link, str):
        return False

    # لو العميل بيطلب متابعين
    if "متابعين" in service_type:
        if "facebook.com/" in link and not any(x in link for x in ["/posts/", "/videos/", "/reel/", "/story/"]):
            return True
        if "instagram.com/" in link and ("/" in link.strip("/") and not any(x in link for x in ["/p/", "/reel/", "/stories/"])):
            return True
        if "tiktok.com/@" in link and "/video/" not in link:
            return True
        if "kwai" in link and "/video/" not in link:
            return True

    # لو العميل بيطلب لايكات أو مشاهدات أو تعليقات
    if any(x in service_type for x in ["لايكات", "مشاهدات", "تعليقات"]):
        if any(x in link for x in ["/posts/", "/videos/", "/reel/", "/story/", "/p/", "/reel/", "/video/"]):
            return True

    # خدمات اليوتيوب
    if "يوتيوب" in service_type:
        if "youtube.com" in link or "youtu.be" in link:
            return True

    return False
