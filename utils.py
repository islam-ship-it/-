def extract_image_url_from_message(data):
    """
    محاولة استخراج رابط الصورة من بيانات رسالة ZAPI
    """
    try:
        image_data = data.get("image", {})
        
        direct_url = image_data.get("url") or image_data.get("link")
        
        if direct_url:
            print(f"✅ رابط الصورة موجود مباشرة: {direct_url}")
            return direct_url

        # نبحث داخل body لو فيه لينك صورة
        body = data.get("body", "")
        if isinstance(body, str) and "http" in body:
            print(f"✅ رابط الصورة موجود داخل body: {body}")
            return body

        print("⚠ لم يتم العثور على رابط صورة واضح.")
        
    except Exception as e:
        print(f"❌ استثناء أثناء قراءة بيانات الصورة: {e}")

    return None
