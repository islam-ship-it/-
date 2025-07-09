import os
import time
import json
import requests
import threading
import traceback
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

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

# Assistant ID للنموذج الأرخص (مثلاً GPT-4o Mini أو GPT-3.5-turbo) - تم التعليق عليه مؤقتاً
ASSISTANT_ID_CHEAPER = os.getenv("ASSISTANT_ID_CHEAPER") 

# عدد الرسائل المسموح بها للنموذج الأغلى قبل التحويل للأرخص - تم التعليق عليه مؤقتاً
MAX_MESSAGES_FOR_PREMIUM_MODEL = int(os.getenv("MAX_MESSAGES_FOR_PREMIUM_MODEL", 10)) 

MONGO_URI = os.getenv("MONGO_URI")

# متغيرات بيئة جديدة للمتابعة
FOLLOW_UP_INTERVAL_MINUTES = int(os.getenv("FOLLOW_UP_INTERVAL_MINUTES", 1440)) # كل 24 ساعة = 1440 دقيقة
MAX_FOLLOW_UPS = int(os.getenv("MAX_FOLLOW_UPS", 3)) # 3 رسائل متابعة كحد أقصى

# ==============================================================================
# إعدادات قاعدة البيانات (MongoDB)
# ==============================================================================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["whatsapp_bot"]
    sessions_collection = db["sessions"]
    message_queue_collection = db["message_queue"] # جديد: كوليكشن لطابور الرسائل
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
# متغيرات عالمية لإدارة الـ Locks
# ==============================================================================
thread_locks = {} # قاموس لتخزين الـ Locks لكل thread_id في OpenAI
# client_processing_locks لم تعد ضرورية بنفس الشكل بعد استخدام طابور MongoDB

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
            "name": "",
            "last_message_time": datetime.utcnow().isoformat(), # جديد: آخر وقت رسالة
            "follow_up_sent": 0, # جديد: عدد رسائل المتابعة المرسلة
            "follow_up_status": "none", # جديد: حالة المتابعة
            "last_follow_up_time": None, # جديد: لتسجيل آخر وقت تم فيه إرسال رسالة متابعة
            "payment_status": "pending" # جديد: حالة الدفع (pending, confirmed, cancelled)
        }
    else:
        # التأكد من وجود جميع المفاتيح الافتراضية في الجلسات القديمة
        session.setdefault("history", [])
        session.setdefault("thread_id", None)
        session.setdefault("message_count", 0)
        session.setdefault("name", "")
        session.setdefault("last_message_time", datetime.utcnow().isoformat())
        session.setdefault("follow_up_sent", 0)
        session.setdefault("follow_up_status", "none")
        session.setdefault("last_follow_up_time", None)
        session.setdefault("payment_status", "pending") # جديد
    return session

def save_session(user_id, session_data):
    """
    يحفظ أو يحدث بيانات جلسة المستخدم في قاعدة البيانات.
    """
    session_data["_id"] = user_id
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)
    print(f"💾 تم حفظ بيانات الجلسة للعميل {user_id}.", flush=True)

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
# دالة تحويل الصوت إلى نص (Speech-to-Text)
# ==============================================================================
def transcribe_audio(audio_url, file_format="ogg"):
    """
    يحمل ملف صوتي من URL ويحوله إلى نص باستخدام OpenAI Whisper API.
    """
    print(f"🎙️ محاولة تحميل وتحويل الصوت من: {audio_url}", flush=True)
    try:
        # تحميل ملف الصوت
        audio_response = requests.get(audio_url, stream=True)
        audio_response.raise_for_status() # يرفع استثناء للأكواد 4xx/5xx

        # حفظ الملف مؤقتاً
        temp_audio_file = f"temp_audio_{int(time.time())}.{file_format}"
        with open(temp_audio_file, "wb") as f:
            for chunk in audio_response.iter_content(chunk_size=8192):
                f.write(f)
        print(f"✅ تم تحميل الملف الصوتي: {temp_audio_file}", flush=True)

        # تحويل الصوت إلى نص باستخدام OpenAI Whisper API
        with open(temp_audio_file, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        transcribed_text = transcription.text
        print(f"📝 تم تحويل الصوت إلى نص: {transcribed_text}", flush=True)
        return transcribed_text

    except requests.exceptions.RequestException as e:
        print(f"❌ خطأ في تحميل الملف الصوتي: {e}", flush=True)
    except Exception as e:
        print(f"❌ خطأ أثناء تحويل الصوت إلى نص: {e}", flush=True)
        traceback.print_exc()
    finally:
        # حذف الملف المؤقت بعد الانتهاء
        if 'temp_audio_file' in locals() and os.path.exists(temp_audio_file):
            os.remove(temp_audio_file)
            print(f"🗑️ تم حذف الملف الصوتي المؤقت: {temp_audio_file}", flush=True)
    return None

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

    # تحديد الـ Assistant ID: دائماً نستخدم النموذج الأغلى حالياً
    current_assistant_id = ASSISTANT_ID_PREMIUM 
    print(f"✅ العميل {sender_id} يستخدم النموذج الأساسي (Assistant ID: {current_assistant_id})", flush=True)

    # إضافة رسالة المستخدم إلى الـ history (فقط إذا كانت رسالة من العميل)
    # رسائل المتابعة لن تزيد الـ message_count
    is_internal_follow_up = False
    if isinstance(content, list):
        for item in content:
            if item.get("type") == "text" and "رسالة متابعة داخلية" in item.get("text", ""):
                is_internal_follow_up = True
                break
    
    if not is_internal_follow_up:
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
                        # لا نضيف رد المساعد للـ history إذا كانت رسالة متابعة داخلية
                        if not is_internal_follow_up:
                            session["history"].append({"role": "assistant", "content": reply})
                            session["history"] = session["history"][-10:] # الاحتفاظ بآخر 10 إدخالات فقط
                            save_session(sender_id, session) # حفظ الجلسة بعد إضافة رد المساعد
                        # ------------------------------------------

                        return reply
                    else:
                        print(f"⚠️ رد المساعد لا يحتوي على نص متوقع: {msg_obj.content}", flush=True)
                        # حفظ الجلسة حتى لو الرد غير متوقع
                        if not is_internal_follow_up:
                            session["history"].append({"role": "assistant", "content": "⚠ مشكلة في استلام رد المساعد."})
                            session["history"] = session["history"][-10:]
                            save_session(sender_id, session)
                        return "⚠ مشكلة في استلام رد المساعد، حاول تاني."

    except Exception as e:
        print(f"❌ حصل استثناء أثناء الإرسال للمساعد أو استلام الرد: {e}", flush=True)
        traceback.print_exc() # طباعة الـ traceback كامل للتشخيص
        # حفظ الجلسة حتى لو حصل استثناء
        if not is_internal_follow_up:
            session["history"].append({"role": "assistant", "content": "⚠ حدث خطأ عام."})
            session["history"] = session["history"][-10:]
            save_session(sender_id, session)
    finally:
        pass # الـ Lock يتم تحريره تلقائياً بواسطة 'with'

    return "⚠ مشكلة مؤقتة، حاول تاني."

# ==============================================================================
# دالة المتابعة (Follow-up Function)
# ==============================================================================
def send_follow_up_message(user_id):
    """
    تقوم بطلب من المساعد صياغة رسالة متابعة وإرسالها للعميل.
    """
    session = get_session(user_id)
    name = session.get("name", "عميل")
    follow_up_count = session.get("follow_up_sent", 0) + 1 # رقم رسالة المتابعة اللي هنبعتها دلوقتي

    # تخصيص الـ prompt للمساعد بناءً على رقم رسالة المتابعة
    if follow_up_count == 1:
        prompt_text = f"رسالة متابعة داخلية: العميل {name} لم يتفاعل منذ فترة. صغ رسالة متابعة ودودة ومشجعة تذكره بخدماتنا وتدعوه لإكمال المحادثة أو الشراء. اجعلها قصيرة ومباشرة. لا تطلب منه معلومات شخصية. لا تنهي المحادثة."
    elif follow_up_count == 2:
        prompt_text = f"رسالة متابعة داخلية: العميل {name} لم يتفاعل بعد رسالة المتابعة الأولى. صغ رسالة متابعة ثانية أكثر إلحاحًا ولكن لا تزال ودودة، تذكره بقيمة خدماتنا وتدعوه لاتخاذ قرار. اجعلها قصيرة ومباشرة. لا تطلب منه معلومات شخصية. لا تنهي المحادثة."
    elif follow_up_count == 3:
        prompt_text = f"رسالة متابعة داخلية: العميل {name} لم يتفاعل بعد رسالتي المتابعة. صغ رسالة متابعة أخيرة، تذكره بآخر فرصة أو عرض خاص (إذا كان هناك) وتدعوه لاتخاذ قرار نهائي. اجعلها قصيرة ومباشرة. لا تطلب منه معلومات شخصية. لا تنهي المحادثة."
    else:
        # لو حصل أي خطأ ووصلنا هنا، نستخدم رسالة عامة
        prompt_text = f"رسالة متابعة داخلية: العميل {name} لم يتفاعل منذ فترة. صغ رسالة متابعة ودودة ومشجعة تذكره بخدماتنا وتدعوه لإكمال المحادثة أو الشراء. اجعلها قصيرة ومباشرة. لا تطلب منه معلومات شخصية. لا تنهي المحادثة."

    print(f"🕵️‍♂️ جاري طلب رسالة متابعة رقم {follow_up_count} للعميل {user_id} ({name}) من المساعد.", flush=True)
    try:
        # استدعاء ask_assistant مع الـ prompt الداخلي
        # ask_assistant ستحدد الـ Assistant ID بناءً على message_count
        follow_up_reply = ask_assistant([{"type": "text", "text": prompt_text}], user_id, name) # تم تصحيح هنا

        if follow_up_reply and "⚠" not in follow_up_reply: # تأكد أن الرد ليس رسالة خطأ
            send_message(user_id, follow_up_reply)

            # تحديث حالة المتابعة في الجلسة
            session["follow_up_sent"] = follow_up_count # تحديث العدد
            session["follow_up_status"] = f"sent_{follow_up_count}"
            session["last_follow_up_time"] = datetime.utcnow().isoformat() # تحديث وقت آخر متابعة
            save_session(user_id, session)
            print(f"✅ تم إرسال رسالة المتابعة رقم {follow_up_count} للعميل {user_id}.", flush=True)
        else:
            print(f"❌ المساعد لم يتمكن من صياغة رسالة متابعة للعميل {user_id}. الرد: {follow_up_reply}", flush=True)

    except Exception as e:
        print(f"❌ خطأ أثناء إرسال رسالة المتابعة للعميل {user_id}: {e}", flush=True)
        traceback.print_exc()

# ==============================================================================
# دالة Worker لمعالجة طابور الرسائل
# ==============================================================================
def message_queue_worker():
    print("👷‍♂️ Worker بدأ تشغيل معالجة طابور الرسائل.", flush=True)
    while True:
        try:
            # البحث عن رسالة "pending" في الطابور (أقدم رسالة أولاً)
            message_doc = message_queue_collection.find_one_and_update(
                {"status": "pending"},
                {"$set": {"status": "processing", "processing_start_time": datetime.utcnow()}},
                sort=[("timestamp", 1)] # أقدم رسالة أولاً
            )

            if message_doc:
                sender = message_doc["sender"]
                name = message_doc["name"]
                msg_type = message_doc["msg_type"]
                
                content_to_assistant = None
                
                print(f"⚙️ Worker يعالج رسالة من {sender} (النوع: {msg_type}).", flush=True)

                if msg_type == "audio":
                    audio_url = message_doc["audio_url"]
                    audio_mime_type = message_doc["audio_mime_type"]
                    transcribed_text = transcribe_audio(audio_url, file_format="ogg") # ZAPI بيبعت ogg
                    if transcribed_text:
                        content_to_assistant = [{"type": "text", "text": f"رسالة صوتية من العميل {name} ({sender}):\n{transcribed_text}"}]
                    else:
                        send_message(sender, "عذراً، لم أتمكن من فهم رسالتك الصوتية. هل يمكنك كتابتها من فضلك؟")
                        message_queue_collection.delete_one({"_id": message_doc["_id"]}) # حذف الرسالة من الطابور
                        continue # تخطي باقي المعالجة
                elif msg_type == "image":
                    image_url = message_doc["image_url"]
                    caption = message_doc["caption"]
                    content_to_assistant = [
                        {"type": "text", "text": f"صورة من العميل {name} ({sender})."},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                    if caption:
                        content_to_assistant.append({"type": "text", "text": f"تعليق على الصورة:\n{caption}"})
                elif msg_type == "text":
                    content_to_assistant = [{"type": "text", "text": message_doc["content"]}]
                
                if content_to_assistant:
                    reply = ask_assistant(content_to_assistant, sender, name)
                    if reply:
                        send_message(sender, reply)
                
                # حذف الرسالة من الطابور بعد المعالجة بنجاح
                message_queue_collection.delete_one({"_id": message_doc["_id"]})
                print(f"✅ تم معالجة وحذف الرسالة من الطابور للعميل {sender}.", flush=True)
            else:
                # لا توجد رسائل في الطابور، انتظر قليلاً قبل التحقق مرة أخرى
                time.sleep(0.5) # ممكن تخليها 0.5 أو 1 ثانية حسب سرعة المعالجة
        except Exception as e:
            print(f"❌ خطأ في Worker معالجة طابور الرسائل: {e}", flush=True)
            traceback.print_exc()
            # في حالة الخطأ، ممكن نرجع حالة الرسالة لـ "pending" أو "failed"
            # عشان متتجاهلش، أو نضيف عداد محاولات
            if message_doc:
                message_queue_collection.update_one(
                    {"_id": message_doc["_id"]},
                    {"$set": {"status": "failed", "error_message": str(e)}}
                )
            time.sleep(5) # انتظر فترة أطول بعد الخطأ لتجنب تكرار الأخطاء بسرعة

# ==============================================================================
# دالة الجدولة التي تبحث عن العملاء المترددين
# ==============================================================================
def check_for_inactive_users():
    print("🔍 جاري البحث عن عملاء مترددين...", flush=True)
    current_time = datetime.utcnow()
    
    # البحث عن الجلسات التي:
    # 1. لم تتفاعل منذ فترة (أقدم من FOLLOW_UP_INTERVAL_MINUTES)
    # 2. لم يتم إرسال الحد الأقصى من رسائل المتابعة لها
    # 3. لم يتم إرسال رسالة متابعة لها في الفترة الحالية (عشان نمنع التكرار)
    # 4. حالة الدفع ليست "confirmed" (لم يدفع بعد)
    
    inactive_sessions = sessions_collection.find({
        "last_message_time": {
            "$lt": (current_time - timedelta(minutes=FOLLOW_UP_INTERVAL_MINUTES)).isoformat()
        },
        "follow_up_sent": {
            "$lt": MAX_FOLLOW_UPS
        },
        "$or": [
            {"last_follow_up_time": None}, # لو لسه متبعتلوش أي رسالة متابعة
            {"last_follow_up_time": {
                "$lt": (current_time - timedelta(minutes=FOLLOW_UP_INTERVAL_MINUTES)).isoformat()
            }}
        ],
        "payment_status": {"$ne": "confirmed"} # جديد: استبعاد العملاء اللي دفعوا
    })

    for session in inactive_sessions:
        user_id = session["_id"]
        send_follow_up_message(user_id)

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
    # إعداد الجدولة (لرسائل المتابعة)
    scheduler = BackgroundScheduler()
    # تشغيل check_for_inactive_users كل 5 دقائق
    scheduler.add_job(check_for_inactive_users, 'interval', minutes=5) 
    scheduler.start()
    print("⏰ تم بدء الجدولة بنجاح.", flush=True)

    # تشغيل Worker لمعالجة طابور الرسائل في Thread منفصل
    worker_thread = threading.Thread(target=message_queue_worker, daemon=True)
    worker_thread.start()
    print("👷‍♂️ تم بدء Worker معالجة طابور الرسائل.", flush=True)

    app.run(host="0.0.0.0", port=5000, debug=True)
