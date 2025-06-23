# session_storage.py

# ده ملف لتخزين واسترجاع المحادثات الخاصة بكل عميل (user_id)
# وبيستخدم Dictionary مؤقت في الذاكرة، وممكن نطوره لاحقًا ليخزن في قاعدة بيانات أو ملف

session_memory = {}

def get_session(user_id):
    """استرجع المحادثة الحالية الخاصة بالمستخدم"""
    return session_memory.get(user_id, [])

def save_session(user_id, messages):
    """خزن المحادثة الحالية للمستخدم"""
    session_memory[user_id] = messages

def reset_session(user_id):
    """امسح المحادثة الخاصة بالمستخدم"""
    session_memory.po
