import os
import time
import json
import requests
import threading
import traceback
import random
import asyncio
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# Telegram imports
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# --- (كل إعدادات البيئة وقاعدة البيانات تبقى كما هي) ---
# ... (سأختصرها هنا ولكنها موجودة في الكود الكامل أدناه) ...

# الكود الكامل يبدأ هنا
# ==============================================================================
# تحميل متغيرات البيئة
# ==============================================================================
load_dotenv()

# ==============================================================================
# إعدادات البيئة
# ==============================================================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL")
ZAPI_INSTANCE_ID = os.getenv("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN")
CLIENT_TOKEN = os.getenv("CLIENT_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
FOLLOW_UP_INTERVAL_MINUTES = int(os.getenv("FOLLOW_UP_INTERVAL_MINUTES", 1440))
MAX_FOLLOW_UPS = int(os.getenv("MAX_FOLLOW_UPS", 3))

# ==============================================================================
# التحقق من المتغيرات الأساسية
# ==============================================================================
if not all([OPENAI_API_KEY, ASSISTANT_ID_PREMIUM, TELEGRAM_BOT_TOKEN, MONGO_URI]):
    print("❌ خطأ فادح: واحد أو أكثر من متغيرات البيئة الأساسية غير موجود.")

# ==============================================================================
# إعدادات قاعدة البيانات (MongoDB)
# ==============================================================================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    print("✅ تم الاتصال بقاعدة البيانات بنجاح.", flush=True)
except Exception as e:
    print(f"❌ فشل الاتصال بقاعدة البيانات: {e}", flush=True)
    exit()

# ==============================================================================
# إعداد تطبيق Flask وعميل OpenAI
# ==============================================================================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

# ==============================================================================
# منطق Telegram (Webhook) - الإصدار الجديد والمستقر
# ==============================================================================

# دالة إعداد الـ Webhook (تعمل مرة واحدة فقط)
async def setup_telegram_webhook():
    render_hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME')
    if render_hostname:
        print("🔧 جاري إعداد Webhook تيليجرام...", flush=True)
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        webhook_url = f"https://{render_hostname}/{TELEGRAM_BOT_TOKEN}"
        await bot.set_webhook(
            url=webhook_url,
            allowed_updates=[
                "message",
                "edited_message",
                "business_message",
                "edited_business_message",
                "deleted_business_messages"
            ]
        )
        print(f"✅ [Telegram] تم إعداد الـ Webhook بنجاح على: {webhook_url}", flush=True)
    else:
        print("⚠️ لم يتم العثور على RENDER_EXTERNAL_HOSTNAME. تخطي إعداد الـ Webhook.", flush=True)

# نقوم بتشغيل دالة الإعداد عند بدء تشغيل السيرفر
try:
    print("⏳ محاولة إعداد Webhook تيليجرام...", flush=True)
    asyncio.run(setup_telegram_webhook())
except Exception as e:
    print(f"❌ فشل إعداد تيليجرام أثناء بدء التشغيل: {e}", flush=True)

# ==============================================================================
# المسار الرئيسي ونظام الجدولة
# ==============================================================================
@app.route("/", methods=["GET"])
def home():
    return "✅ السيرفر يعمل (واتساب و تيليجرام)."

def check_for_inactive_users():
    pass 

scheduler = BackgroundScheduler()
# scheduler.add_job(check_for_inactive_users, 'interval', minutes=5)
scheduler.start()
print("⏰ تم بدء الجدولة بنجاح.", flush=True)

# ==============================================================================
# تشغيل التطبيق
# ==============================================================================
if __name__ == "__main__":
    # هذا الجزء يستخدم فقط للاختبار المحلي المباشر بدون Gunicorn
    print("🚀 جاري بدء تشغيل السيرفر للاختبار المحلي (لا تستخدم هذا في الإنتاج)...")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
