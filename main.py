import os
import time
import json
import requests
import threading
import traceback # لاستخدام traceback.print_exc()
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timedelta

# ==============================================================================
# إعدادات البيئة (تأكد من ضبطها بشكل صحيح في ملف .env)
# ==============================================================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")

# Assistant ID للنموذج الأغلى (GPT-4o) - يتم قراءته من .env
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM") 

# Assistant ID للنموذج الأرخص (مثلاً GPT-4o Mini أو GPT-3.5-turbo) - يتم قراءته من .env
ASSISTANT_ID_CHEAPER = os.getenv("ASSISTANT_ID_CHEAPER") 

# عدد الرسائل المسموح بها للنموذج الأغلى قبل التحويل للأرخص - يتم قراءته من .env
# يتم تحويله إلى عدد صحيح، مع قيمة افتراضية 10 إذا لم يتم تعيينه
MAX_MESSAGES_FOR_PREMIUM_MODEL = int(os.getenv("MAX_MESSAGES_FOR_PREMIUM_MODEL", 10)) 

MONGO_URI = os.getenv("MONGO_URI")

# ==============================================================================
# إعدادات قاعدة البيانات (MongoDB)
# ==============================================================================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["whatsapp_bot"]
    sessions_collection = db["sessions"]
    print("✅ تم الاتصال بقاعدة البيانات بنجاح.", flush=True)
except Exception as e:
    print(f"❌ فشل الاتصال بقاعدة البيانات: {e}", flush=True)
    # يمكنك هنا اختيار إيقاف التطبيق أو التعامل مع الخطأ بطريقة أخرى

# ==============================================================================
# إعداد تطبيق Flask وعميل OpenAI
# ==============================================================================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

# ==============================================================================
# متغيرات عالمية لإدارة الرسائل المعلقة والمؤقتات والـ Locks
# ==============================================================================
pending_messages = {}
timers = {}
thread_locks = {} # جديد: قاموس لتخزين الـ Locks لكل thread_id في OpenAI

# ==============================================================================
# دوال إدارة الجلسات
# ==============================================================================
def get_session(user_id):
    """
    يسترجع بيانات جلسة المستخدم من قاعدة البيانات أو ينشئ جلسة جديدة.
    """
    session = sessions_collection.find_one({"_id": user_id})
    if not session:
        session = {
            "_id": user_id,
            "history": [],
            "thread_id": None,
            "message_count": 0,
            # "block_until": None # تم إزالة هذا المفتاح لأن خاصية الحظر تم إلغاؤها
        }
    else:
        # التأكد من وجود جميع المفاتيح الافتراضية في الجلسات القديمة
        session.setdefault("history", [])
        session.setdefault("thread_id", None)
        session.setdefault("message_count", 0)
        session.setdefault("name", "")
        # session.pop("block_until", None) # إزالة block_until من الجلسات القديمة إذا كانت موجودة
    return session

def save_session(user_id, session_data):
    """
    يحفظ أو يحدث بيانات جلسة المستخدم في قاعدة البيانات.
    """
    session_data["_id"] = user_id
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)
    print(f"💾 تم حفظ بيانات الجلسة للعميل {user_id}.", flush=True)

# تم حذف دالة block_client_24h بالكامل لأن خاصية الحظر تم إلغاؤها

# ==============================================================================
# دالة إرسال الرسائل عبر ZAPI
# ==============================================================================
def send_message(phone, message):
    """
    يرسل رسالة نصية إلى رقم هاتف محدد باستخدام ZAPI.
    """
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        print(f"📤 تم إرسال رسالة للعميل {phone}، الحالة: {response.status_code}", flush=True)
        response.raise_for_status() # يرفع استثناء للأكواد 4xx/5xx
    except requests.exceptions.RequestException as e:
        print(f"❌ خطأ أثناء إرسال الرسالة عبر ZAPI: {e}", flush=True)
    except Exception as e:
        print(f"❌ خطأ غير متوقع أثناء إرسال الرسالة: {e}", flush=True)

# ==============================================================================
# دالة التفاعل مع مساعد OpenAI
# ==============================================================================
def ask_assistant(content, sender_id, name=""):
    """
    يرسل المحتوى إلى مساعد OpenAI ويسترجع الرد.
    """
    session = get_session(sender_id)

    # تحديث اسم المستخدم إذا كان متاحاً ولم يتم حفظه من قبل
    if name and not session.get("name"):
        session["name"] = name
    
    # إنشاء Thread جديد إذا لم يكن موجوداً للجلسة
    if not session.get("thread_id"):
        try:
            thread = client.beta.threads.create()
            session["thread_id"] = thread.id
            print(f"🆕 تم إنشاء Thread جديد للمستخدم {sender_id}: {thread.id}", flush=True)
        except Exception as e:
            print(f"❌ فشل إنشاء Thread جديد: {e}", flush=True)
            return "⚠ مشكلة مؤقتة في إنشاء المحادثة، حاول تاني."

    # تحديد الـ Assistant ID بناءً على عدد الرسائل
    # إذا كان عدد الرسائل أكبر من أو يساوي الحد المسموح به للنموذج الأغلى
    # وتم توفير ASSISTANT_ID_CHEAPER
    if session["message_count"] >= MAX_MESSAGES_FOR_PREMIUM_MODEL and ASSISTANT_ID_CHEAPER:
        current_assistant_id = ASSISTANT_ID_CHEAPER
        print(f"🔄 تحويل العميل {sender_id} إلى النموذج الأرخص (Assistant ID: {current_assistant_id})", flush=True)
    else:
        current_assistant_id = ASSISTANT_ID_PREMIUM # الافتراضي هو النموذج الأغلى
        print(f"✅ العميل {sender_id} يستخدم النموذج الأساسي (Assistant ID: {current_assistant_id})", flush=True)

    # إضافة رسالة المستخدم إلى الـ history
    # ملاحظة: session["message_count"] تم زيادته بالفعل في بداية الدالة
    session["message_count"] += 1 # زيادة العداد هنا قبل إرسال الرسالة
    session["history"].append({"role": "user", "content": content})
    # لا تحفظ هنا، سنحفظ بعد إضافة رد المساعد

    print(f"\n🚀 الداتا داخلة للمساعد (OpenAI):\n{json.dumps(content, indent=2, ensure_ascii=False)}", flush=True)

    # ==========================================================================
    # تطبيق الـ Lock لمنع تداخل الـ Runs على نفس الـ Thread
    # ==========================================================================
    # التأكد من وجود Lock لهذا الـ thread_id
    if session["thread_id"] not in thread_locks:
        thread_locks[session["thread_id"]] = threading.Lock()

    # استخدام الـ Lock لضمان run واحد فقط في نفس الوقت
    try:
        with thread_locks[session["thread_id"]]:
            # إضافة الرسالة إلى Thread في OpenAI
            client.beta.threads.messages.create(
                thread_id=session["thread_id"],
                role="user",
                content=content
            )
            print(f"✅ تم إرسال الداتا للمساعد بنجاح.", flush=True)

            # تشغيل المساعد لمعالجة الرسالة باستخدام الـ ID المحدد
            run = client.beta.threads.runs.create(thread_id=session["thread_id"], assistant_id=current_assistant_id)
            print(f"🏃‍♂️ تم بدء Run للمساعد: {run.id} باستخدام {current_assistant_id}", flush=True)

            # انتظار اكتمال الـ Run
            while True:
                run_status = client.beta.threads.runs.retrieve(thread_id=session["thread_id"], run_id=run.id)
                print(f"⏳ حالة الـ Run: {run_status.status}", flush=True)
                if run_status.status == "completed":
                    break
                elif run_status.status in ["failed", "cancelled", "expired"]:
                    print(f"❌ الـ Run فشل أو تم إلغاؤه/انتهت صلاحيته: {run_status.status}", flush=True)
                    # حفظ الجلسة حتى لو فشل الـ Run لتحديث حالة الـ history
                    session["history"].append({"role": "assistant", "content": "⚠ حدث خطأ أثناء معالجة طلبك."})
                    session["history"] = session["history"][-10:]
                    save_session(sender_id, session)
                    return "⚠ حدث خطأ أثناء معالجة طلبك، حاول تاني."
                time.sleep(2) # انتظار ثانيتين قبل التحقق مرة أخرى

            # استرجاع رسائل المساعد
            messages = client.beta.threads.messages.list(thread_id=session["thread_id"])
            
            # البحث عن أحدث رد من المساعد
            for msg_obj in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
                if msg_obj.role == "assistant":
                    # التأكد من أن الرد يحتوي على نص قبل محاولة الوصول إلى .text.value
                    if msg_obj.content and hasattr(msg_obj.content[0], 'text') and hasattr(msg_obj.content[0].text, 'value'):
                        reply = msg_obj.content[0].text.value.strip()
                        print(f"💬 رد المساعد:\n{reply}", flush=True)
                        
                        # --- إضافة رد المساعد إلى الـ history هنا ---
                        session["history"].append({"role": "assistant", "content": reply})
                        session["history"] = session["history"][-10:] # الاحتفاظ بآخر 10 إدخالات فقط
                        save_session(sender_id, session) # حفظ الجلسة بعد إضافة رد المساعد
                        # ------------------------------------------

                        # تم إزالة جزء الحظر بالكامل
                        return reply
                    else:
                        print(f"⚠️ رد المساعد لا يحتوي على نص متوقع: {msg_obj.content}", flush=True)
                        # حفظ الجلسة حتى لو الرد غير متوقع
                        session["history"].append({"role": "assistant", "content": "⚠ مشكلة في استلام رد المساعد."})
                        session["history"] = session["history"][-10:]
                        save_session(sender_id, session)
                        return "⚠ مشكلة في استلام رد المساعد، حاول تاني."

    except Exception as e:
        print(f"❌ حصل استثناء أثناء الإرسال للمساعد أو استلام الرد: {e}", flush=True)
        traceback.print_exc() # طباعة الـ traceback كامل للتشخيص
        # حفظ الجلسة حتى لو حصل استثناء
        session["history"].append({"role": "assistant", "content": "⚠ حدث خطأ عام."})
        session["history"] = session["history"][-10:]
        save_session(sender_id, session)
    finally:
        # محاولة إزالة الـ Lock من القاموس بعد الانتهاء
        # يجب أن يتم ذلك بحذر لضمان عدم حذف Lock نشط عن طريق الخطأ
        # ولكن في هذا السياق، الـ Lock يتم تحريره تلقائياً بواسطة 'with'
        # هذه الخطوة هنا هي فقط لتنظيف القاموس إذا لم يعد الـ thread_id مستخدماً
        # يمكن تحسينها أكثر في بيئة إنتاجية كبيرة
        if session["thread_id"] in thread_locks:
            # يمكن إضافة منطق للتحقق مما إذا كان الـ Lock لا يزال قيد الاستخدام
            # قبل حذفه من القاموس، ولكن لتبسيط الكود، سنتركه هكذا حالياً
            pass # الـ Lock سيتم تحريره تلقائياً بواسطة 'with'

    return "⚠ مشكلة مؤقتة، حاول تاني."

# ==============================================================================
# دالة معالجة الرسائل النصية المعلقة (تجميع الرسائل)
# ==============================================================================
def process_pending_messages(sender, name):
    """
    تجمع الرسائل النصية الواردة من نفس العميل وترسلها للمساعد كرسالة واحدة.
    """
    print(f"⏳ تجميع رسائل العميل {sender} لمدة 8 ثواني.", flush=True)
    time.sleep(8) # الانتظار لتجميع الرسائل
    
    # دمج جميع الرسائل المعلقة
    combined_text = "\n".join(pending_messages[sender])
    content = [{"type": "text", "text": combined_text}]
    
    print(f"📦 محتوى الرسالة النصية المجمعة المرسل للمساعد:\n{json.dumps(content, indent=2, ensure_ascii=False)}", flush=True)

    reply = ask_assistant(content, sender, name)
    send_message(sender, reply)
    
    # مسح الرسائل المعلقة وإزالة المؤقت
    pending_messages[sender] = []
    timers.pop(sender, None)
    print(f"🎯 الرد تم على جميع رسائل {sender}.", flush=True)

# ==============================================================================
# Webhook لاستقبال الرسائل الواردة
# ==============================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    """
    نقطة نهاية الـ webhook لاستقبال الرسائل من ZAPI.
    """
    data = request.json
    # طباعة البيانات المستلمة كاملة للتشخيص
    print(f"\n📥 البيانات المستلمة كاملة من الـ webhook:\n{json.dumps(data, indent=2, ensure_ascii=False)}", flush=True)

    sender = data.get("phone") or data.get("From")
    msg = data.get("text", {}).get("message") or data.get("body", "")
    # msg_type لم يعد يستخدم لتحديد نوع الصورة، ولكن يمكن الاحتفاظ به لأغراض أخرى
    msg_type = data.get("type", "") 
    name = data.get("pushname") or data.get("senderName") or data.get("profileName") or ""
    
    # استخراج imageUrl مباشرة من الـ data (هذا هو المفتاح لتحديد الصور)
    image_data = data.get("image", {})
    image_url = image_data.get("imageUrl") # سيكون None إذا لم تكن رسالة صورة
    caption = image_data.get("caption", "")

    if not sender:
        print("❌ رقم العميل غير موجود في البيانات المستلمة.", flush=True)
        return jsonify({"status": "no sender"}), 400

    session = get_session(sender)
    # تم إزالة التحقق من حالة الحظر هنا أيضاً
    # if session.get("block_until") and datetime.utcnow() < datetime.fromisoformat(session["block_until"]):
    #     print(f"🚫 العميل {sender} في فترة الحظر.", flush=True)
    #     send_message(sender, "✅ طلبك تحت التنفيذ، نرجو الانتظار.")
    #     return jsonify({"status": "blocked"}), 200

    # ==========================================================================
    # معالجة رسائل الصور (الأولوية الأولى)
    # نتحقق من وجود 'imageUrl' لتحديد ما إذا كانت الرسالة صورة
    # ==========================================================================
    if image_url:
        print(f"🌐 صورة مستلمة (imageUrl: {image_url}, caption: {caption})", flush=True)

        message_content = [
            {"type": "text", "text": f"صورة من العميل {name} ({sender})."},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]
        if caption:
            message_content.append({"type": "text", "text": f"تعليق على الصورة:\n{caption}"})

        # طباعة المحتوى الذي سيتم إرساله إلى ask_assistant للتشخيص
        print(f"📦 محتوى رسالة الصورة المرسل لـ ask_assistant:\n{json.dumps(message_content, indent=2, ensure_ascii=False)}", flush=True)

        reply = ask_assistant(message_content, sender, name)
        if reply: # إرسال الرد فقط إذا كان هناك رد من المساعد
            send_message(sender, reply)
        return jsonify({"status": "image processed"}), 200
    
    # ==========================================================================
    # معالجة الرسائل النصية (إذا لم تكن رسالة صورة)
    # ==========================================================================
    if msg:
        print(f"💬 استقبال رسالة نصية من العميل: {msg}", flush=True)
        if sender not in pending_messages:
            pending_messages[sender] = []
        pending_messages[sender].append(msg)

        # بدء مؤقت لتجميع الرسائل إذا لم يكن هناك مؤقت بالفعل
        if sender not in timers:
            timers[sender] = threading.Thread(target=process_pending_messages, args=(sender, name))
            timers[sender].start()

    return jsonify({"status": "received"}), 200

# ==============================================================================
# نقطة نهاية الصفحة الرئيسية
# ==============================================================================
@app.route("/", methods=["GET"])
def home():
    """
    صفحة رئيسية بسيطة للتحقق من أن السيرفر يعمل.
    """
    return "✅ السيرفر شغال تمام!"

# ==============================================================================
# تشغيل التطبيق
# ==============================================================================
if __name__ == "__main__":
    # تشغيل Flask في وضع التطوير (للتجربة المحلية)
    # في بيئة الإنتاج، استخدم Gunicorn أو ما شابه
    app.run(host="0.0.0.0", port=5000, debug=True) # debug=True مفيد للتشخيص
