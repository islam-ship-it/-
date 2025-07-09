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

ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM") 
ASSISTANT_ID_CHEAPER = os.getenv("ASSISTANT_ID_CHEAPER") 
MAX_MESSAGES_FOR_PREMIUM_MODEL = int(os.getenv("MAX_MESSAGES_FOR_PREMIUM_MODEL", 10)) 

MONGO_URI = os.getenv("MONGO_URI")

FACEBOOK_VERIFY_TOKEN = os.getenv("FACEBOOK_VERIFY_TOKEN")
FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")

FOLLOW_UP_INTERVAL_MINUTES = int(os.getenv("FOLLOW_UP_INTERVAL_MINUTES", 60))
MAX_FOLLOW_UPS = int(os.getenv("MAX_FOLLOW_UPS", 1))

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
            "last_message_time": datetime.utcnow().isoformat(),
            "follow_up_sent": 0,
            "follow_up_status": "none",
            "platform": "unknown" # جديد: لتخزين المنصة التي جاءت منها الرسالة
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
        session.setdefault("platform", "unknown")
    return session

def save_session(user_id, session_data):
    """
    يحفظ أو يحدث بيانات جلسة المستخدم في قاعدة البيانات.
    """
    session_data["_id"] = user_id
    sessions_collection.replace_one({"_id": user_id}, session_data, upsert=True)
    print(f"💾 تم حفظ بيانات الجلسة للعميل {user_id}.", flush=True)

# ==============================================================================
# دوال إرسال الرسائل حسب المنصة
# ==============================================================================
def send_whatsapp_message(phone, message):
    """
    يرسل رسالة نصية إلى رقم هاتف محدد باستخدام ZAPI.
    """
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        print(f"📤 [WhatsApp] تم إرسال رسالة للعميل {phone}، الحالة: {response.status_code}", flush=True)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"❌ [WhatsApp] خطأ أثناء إرسال الرسالة عبر ZAPI: {e}", flush=True)
    except Exception as e:
        print(f"❌ [WhatsApp] خطأ غير متوقع أثناء إرسال الرسالة: {e}", flush=True)

def send_messenger_message(recipient_id, message_text):
    """
    يرسل رسالة نصية إلى عميل ماسنجر.
    """
    url = f"https://graph.facebook.com/v19.0/me/messages?access_token={FACEBOOK_PAGE_ACCESS_TOKEN}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text}
    }
    try:
        response = requests.post(url, headers=headers, json=payload )
        print(f"📤 [Messenger] تم إرسال رسالة ماسنجر للعميل {recipient_id}، الحالة: {response.status_code}", flush=True)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"❌ [Messenger] خطأ أثناء إرسال رسالة ماسنجر: {e}", flush=True)
    except Exception as e:
        print(f"❌ [Messenger] خطأ غير متوقع أثناء إرسال رسالة ماسنجر: {e}", flush=True)

def send_message_to_platform(user_id_in_db, message_text):
    """
    دالة عامة لإرسال الرسائل بناءً على المنصة المخزنة في الجلسة.
    """
    session = get_session(user_id_in_db)
    platform = session.get("platform")
    original_sender_id = user_id_in_db.split('_', 1)[1] # استخراج الـ ID الأصلي

    if platform == "whatsapp":
        send_whatsapp_message(original_sender_id, message_text)
    elif platform == "messenger":
        send_messenger_message(original_sender_id, message_text)
    else:
        print(f"⚠️ لا يمكن إرسال رسالة: منصة غير معروفة للعميل {user_id_in_db}.", flush=True)

# ==============================================================================
# دالة تحويل الصوت إلى نص (Speech-to-Text)
# ==============================================================================
def transcribe_audio(audio_url, file_format="ogg"):
    """
    يحمل ملف صوتي من URL ويحوله إلى نص باستخدام OpenAI Whisper API.
    """
    print(f"🎙️ محاولة تحميل وتحويل الصوت من: {audio_url}", flush=True)
    try:
        audio_response = requests.get(audio_url, stream=True)
        audio_response.raise_for_status()

        temp_audio_file = f"temp_audio_{int(time.time())}.{file_format}"
        with open(temp_audio_file, "wb") as f:
            for chunk in audio_response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"✅ تم تحميل الملف الصوتي: {temp_audio_file}", flush=True)

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
        if 'temp_audio_file' in locals() and os.path.exists(temp_audio_file):
            os.remove(temp_audio_file)
            print(f"🗑️ تم حذف الملف الصوتي المؤقت: {temp_audio_file}", flush=True)
    return None

# ==============================================================================
# دالة التفاعل مع مساعد OpenAI
# ==============================================================================
def ask_assistant(content, user_id_in_db, name=""):
    """
    يرسل المحتوى إلى مساعد OpenAI ويسترجع الرد.
    user_id_in_db هو الـ ID الموحد في قاعدة البيانات (مثلاً whatsapp_201xxxxxx)
    """
    session = get_session(user_id_in_db)

    if name and not session.get("name"):
        session["name"] = name
    
    if not session.get("thread_id"):
        try:
            thread = client.beta.threads.create()
            session["thread_id"] = thread.id
            print(f"🆕 تم إنشاء Thread جديد للمستخدم {user_id_in_db}: {thread.id}", flush=True)
        except Exception as e:
            print(f"❌ فشل إنشاء Thread جديد: {e}", flush=True)
            return "⚠ مشكلة مؤقتة في إنشاء المحادثة، حاول تاني."

    is_internal_follow_up = False
    if isinstance(content, list):
        for item in content:
            if item.get("type") == "text" and "رسالة متابعة داخلية" in item.get("text", ""):
                is_internal_follow_up = True
                break
    
    if not is_internal_follow_up:
        session["message_count"] += 1
        session["history"].append({"role": "user", "content": content})
    
    print(f"\n🚀 الداتا داخلة للمساعد (OpenAI):\n{json.dumps(content, indent=2, ensure_ascii=False)}", flush=True)

    if session["thread_id"] not in thread_locks:
        thread_locks[session["thread_id"]] = threading.Lock()

    try:
        with thread_locks[session["thread_id"]]:
            client.beta.threads.messages.create(
                thread_id=session["thread_id"],
                role="user",
                content=content
            )
            print(f"✅ تم إرسال الداتا للمساعد بنجاح.", flush=True)

            if session["message_count"] >= MAX_MESSAGES_FOR_PREMIUM_MODEL and ASSISTANT_ID_CHEAPER:
                current_assistant_id = ASSISTANT_ID_CHEAPER
                print(f"🔄 تحويل العميل {user_id_in_db} إلى النموذج الأرخص (Assistant ID: {current_assistant_id})", flush=True)
            else:
                current_assistant_id = ASSISTANT_ID_PREMIUM
                print(f"✅ العميل {user_id_in_db} يستخدم النموذج الأساسي (Assistant ID: {current_assistant_id})", flush=True)

            run = client.beta.threads.runs.create(thread_id=session["thread_id"], assistant_id=current_assistant_id)
            print(f"🏃‍♂️ تم بدء Run للمساعد: {run.id} باستخدام {current_assistant_id}", flush=True)

            while True:
                run_status = client.beta.threads.runs.retrieve(thread_id=session["thread_id"], run_id=run.id)
                print(f"⏳ حالة الـ Run: {run_status.status}", flush=True)
                if run_status.status == "completed":
                    break
                elif run_status.status in ["failed", "cancelled", "expired"]:
                    print(f"❌ الـ Run فشل أو تم إلغاؤه/انتهت صلاحيته: {run_status.status}", flush=True)
                    if not is_internal_follow_up:
                        session["history"].append({"role": "assistant", "content": "⚠ حدث خطأ أثناء معالجة طلبك."})
                        session["history"] = session["history"][-10:]
                        save_session(user_id_in_db, session)
                    return "⚠ حدث خطأ أثناء معالجة طلبك، حاول تاني."
                time.sleep(2)

            messages = client.beta.threads.messages.list(thread_id=session["thread_id"])
            
            for msg_obj in sorted(messages.data, key=lambda x: x.created_at, reverse=True):
                if msg_obj.role == "assistant":
                    if msg_obj.content and hasattr(msg_obj.content[0], 'text') and hasattr(msg_obj.content[0].text, 'value'):
                        reply = msg_obj.content[0].text.value.strip()
                        print(f"💬 رد المساعد:\n{reply}", flush=True)
                        
                        if not is_internal_follow_up:
                            session["history"].append({"role": "assistant", "content": reply})
                            session["history"] = session["history"][-10:]
                            save_session(user_id_in_db, session)

                        return reply
                    else:
                        print(f"⚠️ رد المساعد لا يحتوي على نص متوقع: {msg_obj.content}", flush=True)
                        if not is_internal_follow_up:
                            session["history"].append({"role": "assistant", "content": "⚠ مشكلة في استلام رد المساعد."})
                            session["history"] = session["history"][-10:]
                            save_session(user_id_in_db, session)
                        return "⚠ مشكلة في استلام رد المساعد، حاول تاني."

    except Exception as e:
        print(f"❌ حصل استثناء أثناء الإرسال للمساعد أو استلام الرد: {e}", flush=True)
        traceback.print_exc()
        if not is_internal_follow_up:
            session["history"].append({"role": "assistant", "content": "⚠ حدث خطأ عام."})
            session["history"] = session["history"][-10:]
            save_session(user_id_in_db, session)
    finally:
        pass

    return "⚠ مشكلة مؤقتة، حاول تاني."

# ==============================================================================
# دالة المتابعة (Follow-up Function)
# ==============================================================================
def send_follow_up_message(user_id_in_db):
    """
    تقوم بطلب من المساعد صياغة رسالة متابعة وإرسالها للعميل.
    """
    session = get_session(user_id_in_db)
    name = session.get("name", "عميل")

    print(f"🕵️‍♂️ جاري طلب رسالة متابعة للعميل {user_id_in_db} ({name}) من المساعد.", flush=True)
    try:
        internal_prompt = [
            {"type": "text", "text": f"رسالة متابعة داخلية: العميل {name} لم يتفاعل منذ فترة. صغ رسالة متابعة ودودة ومشجعة تذكره بخدماتنا وتدعوه لإكمال المحادثة أو الشراء. اجعلها قصيرة ومباشرة. لا تطلب منه معلومات شخصية. لا تنهي المحادثة."}
        ]
        
        follow_up_reply = ask_assistant(internal_prompt, user_id_in_db, name)

        if follow_up_reply and "⚠" not in follow_up_reply:
            send_message_to_platform(user_id_in_db, follow_up_reply)

            session["follow_up_sent"] += 1
            session["follow_up_status"] = f"sent_{session['follow_up_sent']}"
            save_session(user_id_in_db, session)
            print(f"✅ تم إرسال رسالة المتابعة رقم {session['follow_up_sent']} للعميل {user_id_in_db}.", flush=True)
        else:
            print(f"❌ المساعد لم يتمكن من صياغة رسالة متابعة للعميل {user_id_in_db}. الرد: {follow_up_reply}", flush=True)

    except Exception as e:
        print(f"❌ خطأ أثناء إرسال رسالة المتابعة للعميل {user_id_in_db}: {e}", flush=True)
        traceback.print_exc()

# ==============================================================================
# دالة معالجة الرسائل النصية المعلقة (تجميع الرسائل)
# ==============================================================================
def process_pending_messages(original_sender_id, platform, name):
    """
    تجمع الرسائل النصية الواردة من نفس العميل وترسلها للمساعد كرسالة واحدة.
    """
    user_id_in_db = f"{platform}_{original_sender_id}"
    print(f"⏳ تجميع رسائل العميل {user_id_in_db} لمدة 8 ثواني.", flush=True)
    time.sleep(8)
    
    combined_text = "\n".join(pending_messages[user_id_in_db])
    content = [{"type": "text", "text": combined_text}]
    
    print(f"📦 محتوى الرسالة النصية المجمعة المرسل للمساعد:\n{json.dumps(content, indent=2, ensure_ascii=False)}", flush=True)

    reply = ask_assistant(content, user_id_in_db, name)
    send_message_to_platform(user_id_in_db, reply)
    
    pending_messages[user_id_in_db] = []
    timers.pop(user_id_in_db, None)
    print(f"🎯 الرد تم على جميع رسائل {user_id_in_db}.", flush=True)

# ==============================================================================
# Webhook لاستقبال الرسائل الواردة
# ==============================================================================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode and token:
            if mode == "subscribe" and token == FACEBOOK_VERIFY_TOKEN:
                print("✅ [Webhook] WEBHOOK_VERIFIED (Facebook)", flush=True)
                return challenge, 200
            else:
                return "VERIFICATION_FAILED", 403
        return "OK", 200

    data = request.json
    print(f"\n📥 [Webhook] البيانات المستلمة كاملة من الـ webhook:\n{json.dumps(data, indent=2, ensure_ascii=False)}", flush=True)

    original_sender_id = None
    platform = None
    msg_content = None
    name = ""

    # ==========================================================================
    # معالجة رسائل فيسبوك ماسنجر
    # ==========================================================================
    if data.get("object") == "page":
        platform = "messenger"
        for entry in data["entry"]:
            for messaging_event in entry["messaging"]:
                original_sender_id = messaging_event["sender"]["id"]
                user_id_in_db = f"{platform}_{original_sender_id}"
                name = "Messenger User" # يمكن تحسين هذا للحصول على الاسم الحقيقي

                # تحديث وقت آخر رسالة وحفظ المنصة
                session = get_session(user_id_in_db)
                session["last_message_time"] = datetime.utcnow().isoformat()
                session["platform"] = platform
                save_session(user_id_in_db, session)

                if messaging_event.get("message"):
                    message_text = messaging_event["message"].get("text")
                    if message_text:
                        msg_content = [{"type": "text", "text": message_text}]
                        print(f"💬 [Messenger] رسالة نصية من {original_sender_id}: {message_text}", flush=True)
                    # TODO: إضافة معالجة الصور والملفات الأخرى للماسنجر هنا
                    # if messaging_event["message"].get("attachments"):
                    #     for attachment in messaging_event["message"]["attachments"]:
                    #         if attachment["type"] == "image":
                    #             image_url = attachment["payload"]["url"]
                    #             msg_content = [{"type": "image_url", "image_url": {"url": image_url}}]
                    #             print(f"🌐 [Messenger] صورة من {original_sender_id}: {image_url}", flush=True)
                    #             break
                    #         elif attachment["type"] == "audio":
                    #             audio_url = attachment["payload"]["url"]
                    #             print(f"🎙️ [Messenger] ريكورد صوتي من {original_sender_id}: {audio_url}", flush=True)
                    #             transcribed_text = transcribe_audio(audio_url, file_format="mp4") # الماسنجر ممكن يبعت mp4
                    #             if transcribed_text:
                    #                 msg_content = [{"type": "text", "text": f"رسالة صوتية من {name}:\n{transcribed_text}"}]
                    #             else:
                    #                 send_messenger_message(original_sender_id, "عذراً، لم أتمكن من فهم رسالتك الصوتية.")
                    #                 return jsonify({"status": "audio transcription failed"}), 200
                    #             break
                # TODO: إضافة معالجة postbacks (أزرار) هنا
                # elif messaging_event.get("postback"):
                #     payload = messaging_event["postback"]["payload"]
                #     msg_content = [{"type": "text", "text": f"Payload: {payload}"}]
                #     print(f"🔘 [Messenger] Postback من {original_sender_id}: {payload}", flush=True)

                if msg_content:
                    reply = ask_assistant(msg_content, user_id_in_db, name)
                    if reply:
                        send_message_to_platform(user_id_in_db, reply)
                
        return "EVENT_RECEIVED", 200

    # ==========================================================================
    # معالجة رسائل ZAPI (واتساب)
    # ==========================================================================
    else:
        platform = "whatsapp"
        original_sender_id = data.get("phone") or data.get("From")
        user_id_in_db = f"{platform}_{original_sender_id}"
        msg = data.get("text", {}).get("message") or data.get("body", "")
        name = data.get("pushname") or data.get("senderName") or data.get("profileName") or ""
        
        image_data = data.get("image", {})
        image_url = image_data.get("imageUrl")
        caption = image_data.get("caption", "")

        audio_data = data.get("audio", {})
        audio_url = audio_data.get("audioUrl")
        audio_mime_type = audio_data.get("mimeType")

        if not original_sender_id:
            print("❌ [Webhook] رقم العميل غير موجود في البيانات المستلمة.", flush=True)
            return jsonify({"status": "no sender"}), 400

        # تحديث وقت آخر رسالة وحفظ المنصة
        session = get_session(user_id_in_db)
        session["last_message_time"] = datetime.utcnow().isoformat()
        session["platform"] = platform
        save_session(user_id_in_db, session)
        
        if audio_url:
            print(f"🎙️ [WhatsApp] ريكورد صوتي مستلم (audioUrl: {audio_url}, mimeType: {audio_mime_type})", flush=True)
            transcribed_text = transcribe_audio(audio_url, file_format="ogg")
            if transcribed_text:
                msg_content = [{"type": "text", "text": f"رسالة صوتية من العميل {name} ({original_sender_id}):\n{transcribed_text}"}]
            else:
                print("❌ [WhatsApp] فشل تحويل الريكورد الصوتي إلى نص.", flush=True)
                send_whatsapp_message(original_sender_id, "عذراً، لم أتمكن من فهم رسالتك الصوتية. هل يمكنك كتابتها من فضلك؟")
                return jsonify({"status": "audio transcription failed"}), 200

        elif image_url:
            print(f"🌐 [WhatsApp] صورة مستلمة (imageUrl: {image_url}, caption: {caption})", flush=True)
            msg_content = [
                {"type": "text", "text": f"صورة من العميل {name} ({original_sender_id})."},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]
            if caption:
                msg_content.append({"type": "text", "text": f"تعليق على الصورة:\n{caption}"})

        elif msg:
            print(f"💬 [WhatsApp] استقبال رسالة نصية من العميل: {msg}", flush=True)
            msg_content = [{"type": "text", "text": msg}]
            
        if msg_content:
            # لو الرسالة نصية من الواتساب، هنستخدم تجميع الرسائل
            if isinstance(msg_content[0].get("text"), str) and not audio_url and not image_url:
                if user_id_in_db not in pending_messages:
                    pending_messages[user_id_in_db] = []
                pending_messages[user_id_in_db].append(msg_content[0]["text"])

                if user_id_in_db not in timers:
                    timers[user_id_in_db] = threading.Thread(target=process_pending_messages, args=(original_sender_id, platform, name))
                    timers[user_id_in_db].start()
            else: # لو صورة أو ريكورد، أو نص من الماسنجر، بنبعتها على طول
                reply = ask_assistant(msg_content, user_id_in_db, name)
                if reply:
                    send_message_to_platform(user_id_in_db, reply)

    return jsonify({"status": "received"}), 200

# ==============================================================================
# دالة الجدولة التي تبحث عن العملاء المترددين
# ==============================================================================
def check_for_inactive_users():
    print("🔍 جاري البحث عن عملاء مترددين...", flush=True)
    current_time = datetime.utcnow()
    
    inactive_sessions = sessions_collection.find({
        "last_message_time": {
            "$lt": (current_time - timedelta(minutes=FOLLOW_UP_INTERVAL_MINUTES)).isoformat()
        },
        "follow_up_sent": {
            "$lt": MAX_FOLLOW_UPS
        }
    })

    for session in inactive_sessions:
        user_id_in_db = session["_id"]
        send_follow_up_message(user_id_in_db)

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
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_for_inactive_users, 'interval', minutes=5) 
    scheduler.start()
    print("⏰ تم بدء الجدولة بنجاح.", flush=True)

    app.run(host="0.0.0.0", port=5000, debug=True)
