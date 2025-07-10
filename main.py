
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

# Assistant ID للنموذج الأرخص (مثلاً GPT-4o Mini أو GPT-3.5-turbo) - يتم قراءته من .env
# تم التعليق عليه مؤقتاً بناءً على طلبك
# ASSISTANT_ID_CHEAPER = os.getenv("ASSISTANT_ID_CHEAPER") 

# عدد الرسائل المسموح بها للنموذج الأغلى قبل التحويل للأرخص - يتم قراءته من .env
# تم التعليق عليه مؤقتاً بناءً على طلبك
# MAX_MESSAGES_FOR_PREMIUM_MODEL = int(os.getenv("MAX_MESSAGES_FOR_PREMIUM_MODEL", 10)) 

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
thread_locks = {} # قاموس لتخزين الـ Locks لكل thread_id في OpenAI
client_processing_locks = {} # جديد: Lock لكل عميل عشان نضمن process_pending_messages واحدة بس شغالة

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
                f.write(chunk)
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
            # حفظ الجلسة حتى لو فشل إنشاء Thread
            session["history"].append({"role": "assistant", "content": "⚠ مشكلة مؤقتة في إنشاء المحادثة، حاول تاني."})
            session["history"] = session["history"][-10:]
            save_session(sender_id, session)
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
                
                # ==========================================================================
                # معالجة حالات الـ Run المختلفة (تم إزالة معالجة requires_action التي كانت تسبب المشكلة)
                # ==========================================================================
                if run_status.status == "completed":
                    break
                elif run_status.status in ["failed", "cancelled", "expired"]:
                    print(f"❌ الـ Run فشل أو تم إلغاؤه/انتهت صلاحيته: {run_status.status}", flush=True)
                    # --- التعديل هنا: طباعة تفاصيل الخطأ ---
                    print(f"🚨 تفاصيل Run الفاشل: {json.dumps(run_status.to_dict(), indent=2, ensure_ascii=False)}", flush=True)
                    if run_status.last_error:
                        print(f"🚨 رسالة الخطأ من OpenAI: Code={run_status.last_error.code}, Message={run_status.last_error.message}", flush=True)
                    # ---------------------------------------
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
                        # -------------------------------------------

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
# دالة معالجة الرسائل النصية المعلقة (تجميع الرسائل)
# ==============================================================================
def process_pending_messages(sender, name):
    """
    تجمع الرسائل النصية الواردة من نفس العميل وترسلها للمساعد كرسالة واحدة.
    """
    # التأكد من وجود Lock لهذا العميل
    if sender not in client_processing_locks:
        client_processing_locks[sender] = threading.Lock()

    # استخدام الـ Lock لضمان عملية معالجة واحدة فقط في نفس الوقت لكل عميل
    with client_processing_locks[sender]:
        print(f"⏳ تجميع رسائل العميل {sender} لمدة 8 ثواني.", flush=True)
        time.sleep(8) # الانتظار لتجميع الرسائل
        
        # دمج جميع الرسائل المعلقة
        # التأكد إن فيه رسائل عشان لو الـ thread اشتغل مرتين بالغلط
        if not pending_messages.get(sender):
            print(f"⚠️ لا توجد رسائل معلقة للعميل {sender}، تخطي المعالجة.", flush=True)
            timers.pop(sender, None) # إزالة المؤقت حتى لو مفيش رسائل
            return

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
    name = data.get("pushname") or data.get("senderName") or data.get("profileName") or ""
    
    # استخراج imageUrl مباشرة من الـ data (هذا هو المفتاح لتحديد الصور)
    image_data = data.get("image", {})
    image_url = image_data.get("imageUrl") # سيكون None إذا لم تكن رسالة صورة
    caption = image_data.get("caption", "")

    # استخراج audioUrl مباشرة من الـ data (هذا هو المفتاح لتحديد الريكوردات)
    audio_data = data.get("audio", {})
    audio_url = audio_data.get("audioUrl") # سيكون None إذا لم تكن رسالة صوتية
    audio_mime_type = audio_data.get("mimeType")


    if not sender:
        print("❌ رقم العميل غير موجود في البيانات المستلمة.", flush=True)
        return jsonify({"status": "no sender"}), 400

    session = get_session(sender)
    session["last_message_time"] = datetime.utcnow().isoformat() # تحديث وقت آخر رسالة
    save_session(sender, session) # حفظ الجلسة بعد تحديث الوقت (مهم)
    
    # ==========================================================================
    # معالجة رسائل الريكوردات (الأولوية الأولى)
    # ==========================================================================
    if audio_url:
        print(f"🎙️ ريكورد صوتي مستلم (audioUrl: {audio_url}, mimeType: {audio_mime_type})", flush=True)
        
        # تحويل الريكورد إلى نص
        transcribed_text = transcribe_audio(audio_url, file_format="ogg") # ZAPI بيبعت ogg
        
        if transcribed_text:
            message_content = [{"type": "text", "text": f"رسالة صوتية من العميل {name} ({sender}):\n{transcribed_text}"}]
            print(f"📦 محتوى رسالة الريكورد المرسل لـ ask_assistant:\n{json.dumps(message_content, indent=2, ensure_ascii=False)}", flush=True)
            
            reply = ask_assistant(message_content, sender, name)
            if reply:
                send_message(sender, reply)
            return jsonify({"status": "audio processed"}), 200
        else:
            print("❌ فشل تحويل الريكورد الصوتي إلى نص.", flush=True)
            send_message(sender, "عذراً، لم أتمكن من فهم رسالتك الصوتية. هل يمكنك كتابتها من فضلك؟")
            return jsonify({"status": "audio transcription failed"}), 200

    # ==========================================================================
    # معالجة رسائل الصور (الأولوية الثانية)
    # ==========================================================================
    if image_url:
        print(f"🌐 صورة مستلمة (imageUrl: {image_url}, caption: {caption})", flush=True)

        message_content = [
            {"type": "text", "text": f"صورة من العميل {name} ({sender})."},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]
        if caption:
            message_content.append({"type": "text", "text": f"تعليق على الصورة:\n{caption}"})

        print(f"📦 محتوى رسالة الصورة المرسل لـ ask_assistant:\n{json.dumps(message_content, indent=2, ensure_ascii=False)}", flush=True)

        reply = ask_assistant(message_content, sender, name)
        if reply:
            send_message(sender, reply)
        return jsonify({"status": "image processed"}), 200
    
    # ==========================================================================
    # معالجة الرسائل النصية (الأولوية الثالثة)
    # ==========================================================================
    if msg:
        print(f"💬 استقبال رسالة نصية من العميل: {msg}", flush=True)
        
        # جديد: لو الرسالة النصية تدل على تأكيد دفع
        # يمكن تعديل الكلمات المفتاحية لتكون أكثر دقة
        if "تم" in msg.lower() or "دفعت" in msg.lower() or "تحويل" in msg.lower():
            session = get_session(sender)
            session["payment_status"] = "confirmed"
            save_session(sender, session)
            print(f"💰 تم تأكيد الدفع للعميل {sender}. تم تحديث حالة الدفع.", flush=True)
            # ممكن هنا تبعت رد تلقائي للعميل بتأكيد استلام الدفع
            # send_message(sender, "شكراً لتأكيد الدفع! تم استلام طلبك وسنباشر التنفيذ.")
            
        if sender not in pending_messages:
            pending_messages[sender] = []
        pending_messages[sender].append(msg)

        if sender not in timers:
            timers[sender] = threading.Thread(target=process_pending_messages, args=(sender, name))
            timers[sender].start()

    return jsonify({"status": "received"}), 200

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
            }} # لو آخر رسالة متابعة كانت أقدم من فترة المتابعة
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
    # إعداد الجدولة
    scheduler = BackgroundScheduler()
    # تشغيل check_for_inactive_users كل 5 دقائق
    scheduler.add_job(check_for_inactive_users, 'interval', minutes=5) 
    scheduler.start()
    print("⏰ تم بدء الجدولة بنجاح.", flush=True)

    app.run(host="0.0.0.0", port=5000, debug=True)


