# ده ملف لتخزين واسترجاع المحادثات الخاصة بكل عميل (user_id) + حالة الطلب (status)

session_data = {}

def get_session(user_id):
    if user_id not in session_data:
        session_data[user_id] = {
            "history": [],
            "status": "idle"
        }
    return session_data[user_id]

def save_session(user_id, data):
    session_data[user_id] = data

def reset_session(user_id):
    if user_id in session_data:
        del session_data[user_id]
