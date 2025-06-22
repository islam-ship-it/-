import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os

# إعداد الوصول لجوجل شيت
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(
    os.getenv("GOOGLE_APPLICATION_CREDENTIALS"), scope
)
client = gspread.authorize(creds)

# اسم الجدول في الشيت
SPREADSHEET_NAME = "أسعار"
sheet = client.open(SPREADSHEET_NAME).sheet1

# تحميل الأسعار وتحويلها لقائمة dicts
def load_services():
    records = sheet.get_all_records()
    services = []
    for row in records:
        services.append({
            "platform": row.get("المنصة", "").strip(),
            "type": row.get("النوع", "").strip(),
            "count": int(row.get("العدد", 0)),
            "price": float(row.get("السعر", 0)),
            "audience": row.get("الجمهور", "").strip(),
            "note": row.get("ملاحظة", "").strip()
        })
    return services

# ده اللي بيتم استدعاؤه في main.py
services = load_services()
