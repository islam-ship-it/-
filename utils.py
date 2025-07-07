import requests

def extract_image_url_from_message(data):
    """
    استخراج رابط الصورة سواء موجود مباشرة أو باستخدام media_id
    """
    try:
        # محاولة قراءة الرابط المباشر من بيانات الرسالة
        image_data = data.get("image", {})
        direct_url = image_data.get("url") or image_data.get("link")

        if direct_url:
            print(f"✅ تم العثور على رابط الصورة مباشرة: {direct_url}")
            return direct_url

        # لو مفيش رابط مباشر، نجرب باستخدام media_id
        media_id = image_data.get("id")
        if media_id:
            print(f"📥 جاري محاولة تحميل الصورة باستخدام media_id: {media_id}")
            return download_image_from_zapi(media_id, zapi_token=data.get("zapi_token"))

    except Exception as e:
        print(f"❌ حصل استثناء أثناء استخراج رابط الصورة: {e}")

    print("⚠ لم يتم العثور على رابط الصورة.")
    return None


def download_image_from_zapi(media_id, zapi_token):
    """
    تحميل رابط الصورة من ZAPI باستخدام media_id
    """
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {zapi_token}"}

    print(f"🔧 محاولة تحميل الصورة من ZAPI: {url}")

    try:
        response = requests.get(url, headers=headers)
        print(f"🔧 كود الاستجابة: {response.status_code}")
        print(f"📝 محتوى الرد: {response.text}")

        if response.status_code == 200:
            image_url = response.json().get("url")
            if image_url:
                print(f"✅ تم الحصول على رابط الصورة: {image_url}")
                return image_url
            else:
                print("⚠ لم يتم العثور على رابط الصورة داخل بيانات ZAPI.")

    except Exception as e:
        print(f"❌ خطأ أثناء تحميل الصورة من ZAPI: {e}")

    return None
