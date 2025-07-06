import requests
from config import ZAPI_BASE_URL, ZAPI_INSTANCE_ID, ZAPI_TOKEN, CLIENT_TOKEN

def send_message(phone, message):
    url = f"{ZAPI_BASE_URL}/instances/{ZAPI_INSTANCE_ID}/token/{ZAPI_TOKEN}/send-text"
    headers = {"Content-Type": "application/json", "Client-Token": CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            print(f"📤 رسالة أُرسلت بنجاح إلى {phone}")
        else:
            print(f"⚠ فشل الإرسال - كود: {response.status_code} - التفاصيل: {response.text}")
    except Exception as e:
        print(f"❌ خطأ أثناء إرسال الرسالة: {e}")

def download_image(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ZAPI_TOKEN}"}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get("url")
    except Exception as e:
        print(f"❌ خطأ أثناء تحميل الصورة: {e}")
    return None
