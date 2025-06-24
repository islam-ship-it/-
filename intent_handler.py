import re

def detect_intent(message):
    message = message.lower().strip()

    # عبارات نية الطلب
    if any(word in message for word in ['عايز', 'عاوزه', 'محتاج', 'ممكن', 'سعر', 'كام', 'تكلفة']):
        return 'طلب_خدمة'

    # نية التأكيد بعد ما يوافق
    if any(word in message for word in ['تمام', 'موافق', 'كمل', 'اوكي', 'شغال', 'عاوز أبدأ']):
        return 'تأكيد_طلب'

    # نية إرسال الرابط
    if any(link in message for link in ['http', 'www', '.com', 'facebook.com', 'instagram.com', 'tiktok.com', 'youtu']):
        return 'رابط_الخدمة'

    # نية دفع
    if any(word in message for word in ['حولت', 'دفعت', 'تم الدفع', 'ارسلت', 'سكرين']):
        return 'تأكيد_دفع'

    # كلمات استفسار عن الضمان أو المدة
    if any(word in message for word in ['الضمان', 'هيقعد قد ايه', 'هيوصل امتى', 'بيبدأ امتى']):
        return 'استفسار_عن_الضمان'

    return 'غير_معروف'
