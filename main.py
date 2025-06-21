from flask import Flask, request, jsonify
from flask_cors import CORS
import openai
import os
import requests
import logging
from datetime import datetime

# إعداد نظام السجلات
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # تمكين CORS للسماح بالطلبات من أي مصدر

# التحقق من وجود المتغيرات البيئية المطلوبة
required_env_vars = [
    "OPENAI_API_KEY",
    "ZAPI_BASE_URL", 
    "ZAPI_INSTANCE_ID",
    "ZAPI_TOKEN"
]

missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    logger.error(f"متغيرات بيئية مفقودة: {', '.join(missing_vars)}")
    raise ValueError(f"متغيرات بيئية مطلوبة مفقودة: {', '.join(missing_vars)}")

# إعداد مفاتيح API
client = openai.OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://openai.chatgpt4mena.com/v1")
)

ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")

# إعدادات التطبيق
MAX_HISTORY_LENGTH = int(os.getenv("MAX_HISTORY_LENGTH", "10"))
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4")

# ذاكرة المحادثة (في بيئة الإنتاج، استخدم قاعدة بيانات)
session_memory = {}

def validate_phone_number(phone_number):
    """التحقق من صحة رقم الهاتف"""
    if not phone_number:
        return False
    # إزالة المسافات والرموز الخاصة
    cleaned_phone = ''.join(filter(str.isdigit, phone_number))
    # التحقق من أن الرقم يحتوي على أرقام فقط وطوله مناسب
    return len(cleaned_phone) >= 10 and len(cleaned_phone) <= 15

def sanitize_message(message):
    """تنظيف الرسالة من المحتوى الضار"""
    if not message or not isinstance(message, str):
        return ""
    # إزالة المحتوى الضار المحتمل
    message = message.strip()
    # تحديد طول الرسالة القصوى
    max_length = int(os.getenv("MAX_MESSAGE_LENGTH", "1000"))
    return message[:max_length]

def send_whatsapp_message(phone_number, message):
    """إرسال رسالة واتساب مع معالجة أفضل للأخطاء"""
    if not validate_phone_number(phone_number):
        logger.error(f"رقم هاتف غير صحيح: {phone_number}")
        return False
        
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    payload = {
        "phone": phone_number,
        "message": message
    }
    
    try:
        response = requests.post(
            url, 
            json=payload, 
            timeout=30,  # إضافة timeout
            headers={'Content-Type': 'application/json'}
        )
        
        logger.info(f"[ZAPI] أُرسلت إلى {phone_number}: {response.status_code}")
        
        if response.status_code == 200:
            logger.info(f"[ZAPI] تم الإرسال بنجاح إلى {phone_number}")
            return True
        else:
            logger.error(f"[ZAPI] فشل الإرسال: {response.status_code} - {response.text}")
            return False
            
    except requests.exceptions.Timeout:
        logger.error(f"[ZAPI] انتهت مهلة الإرسال إلى {phone_number}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"[ZAPI] خطأ في الطلب: {e}")
        return False
    except Exception as e:
        logger.error(f"[ZAPI] خطأ غير متوقع: {e}")
        return False

def get_ai_response(messages):
    """الحصول على رد من الذكاء الاصطناعي مع معالجة الأخطاء"""
    try:
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=messages,
            max_tokens=int(os.getenv("MAX_TOKENS", "500")),
            temperature=float(os.getenv("TEMPERATURE", "0.7"))
        )
        return response.choices[0].message.content
    except openai.RateLimitError:
        logger.error("تم تجاوز حد الاستخدام لـ OpenAI")
        return "عذراً، النظام مشغول حالياً. يرجى المحاولة لاحقاً."
    except openai.APIError as e:
        logger.error(f"خطأ في API: {e}")
        return "عذراً، حدث خطأ في النظام. يرجى المحاولة لاحقاً."
    except Exception as e:
        logger.error(f"خطأ غير متوقع في AI: {e}")
        return "عذراً، لا أستطيع الرد حالياً. يرجى المحاولة لاحقاً."

# الصفحة الرئيسية
@app.route('/')
def home():
    """الصفحة الرئيسية للتحقق من حالة البوت"""
    return jsonify({
        "status": "running",
        "message": "✅ البوت يعمل بشكل طبيعي!",
        "timestamp": datetime.now().isoformat(),
        "webhook_url": "/webhook"
    })

# نقطة فحص الصحة
@app.route('/health')
def health_check():
    """فحص صحة التطبيق"""
    try:
        # فحص الاتصال بـ OpenAI
        test_response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1
        )
        ai_status = "healthy"
    except:
        ai_status = "unhealthy"
    
    return jsonify({
        "status": "healthy",
        "ai_service": ai_status,
        "timestamp": datetime.now().isoformat()
    })

# نقطة الاستقبال من ZAPI
@app.route('/webhook', methods=['POST'])
def webhook():
    """معالج webhook لاستقبال رسائل واتساب"""
    try:
        # محاولة قراءة البيانات بطرق مختلفة
        data = None
        
        # محاولة قراءة JSON أولاً
        if request.is_json:
            data = request.get_json()
            logger.info(f"[Webhook] JSON المستلم: {data}")
        else:
            # محاولة قراءة form data
            data = request.form.to_dict()
            logger.info(f"[Webhook] FORM المستلم: {data}")
            
        # إذا لم تنجح الطرق السابقة، محاولة قراءة البيانات الخام
        if not data:
            logger.warning("[Webhook] لم يتم العثور على بيانات صالحة")
            logger.info(f"[RAW DATA] {request.data}")
            return jsonify({"error": "لا توجد بيانات صالحة"}), 400

        # استخراج البيانات المطلوبة
        phone_number = data.get("phone") or data.get("from")
        message = data.get("message") or data.get("text") or data.get("body")
        event_type = data.get("type") # إضافة هذا السطر للحصول على نوع الحدث

        # إذا كان نوع الحدث ليس رسالة، تجاهله (أو عالجه بشكل مختلف)
        if event_type and event_type != "message": # افترض أن 'message' هو النوع لرسائل المستخدم
            logger.info(f"[Webhook] تلقى حدث غير رسالة: {event_type}. تجاهل.")
            return jsonify({"status": "ignored", "message": f"تجاهل حدث من نوع {event_type}"}), 200

        # التحقق من وجود البيانات المطلوبة (بعد تصفية الأحداث غير الرسائل)
        if not phone_number or not message:
            logger.warning("[Webhook] 🚫 بيانات ناقصة! (بعد تصفية الأحداث)")
            logger.info(f"Phone: {phone_number}, Message: {message}")
            return jsonify({"error": "رقم الهاتف أو الرسالة مفقودة"}), 400

        # تنظيف البيانات
        phone_number = phone_number.strip()
        message = sanitize_message(message)
        
        if not message:
            logger.warning("[Webhook] رسالة فارغة بعد التنظيف")
            return jsonify({"error": "رسالة غير صالحة"}), 400

        logger.info(f"[Webhook] معالجة رسالة من {phone_number}: {message[:50]}...")

        # إدارة ذاكرة المحادثة
        history = session_memory.get(phone_number, [])
        history.append({"role": "user", "content": message})
        
        # الاحتفاظ بآخر N رسالة فقط
        session_memory[phone_number] = history[-MAX_HISTORY_LENGTH:]

        # إعداد رسائل النظام والمحادثة
        system_message = {
            "role": "system", 
            "content": os.getenv(
                "SYSTEM_PROMPT", 
                "أنت مساعد ذكي بترد باللهجة المصرية، ودود، منظم، وتجاوب على استفسارات العملاء بشكل احترافي."
            )
        }
        
        messages = [system_message] + session_memory[phone_number]

        # الحصول على رد من الذكاء الاصطناعي
        reply = get_ai_response(messages)
        
        # حفظ رد الذكاء الاصطناعي في الذاكرة
        session_memory[phone_number].append({"role": "assistant", "content": reply})

        # إرسال الرد عبر واتساب
        success = send_whatsapp_message(phone_number, reply)
        
        if success:
            logger.info(f"[Webhook] تم إرسال الرد بنجاح إلى {phone_number}")
            return jsonify({"status": "success", "message": "تم إرسال الرد بنجاح"}), 200
        else:
            logger.error(f"[Webhook] فشل إرسال الرد إلى {phone_number}")
            return jsonify({"status": "error", "message": "فشل إرسال الرد"}), 500

    except Exception as e:
        logger.error(f"[ERROR] خطأ في webhook: {e}", exc_info=True)
        return jsonify({"error": "حدث خطأ داخلي في الخادم"}), 500

# معالج الأخطاء العام
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "الصفحة غير موجودة"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"خطأ داخلي: {error}")
    return jsonify({"error": "خطأ داخلي في الخادم"}), 500

# تشغيل السيرفر
if __name__ == '__main__':
    logger.info("🚀 بدء تشغيل بوت واتساب...")
    app.run(
        host="0.0.0.0", 
        port=int(os.getenv("PORT", "10000")),
        debug=os.getenv("DEBUG", "False").lower() == "true"
    )
