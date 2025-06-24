# message_buffer.py

import time
from threading import Timer

# ذاكرة مؤقتة لتجميع الرسائل
buffers = {}
timers = {}
BUFFER_TIMEOUT = 3  # عدد الثواني المنتظرة لتجميع الرسائل

def add_to_buffer(user_id, message, on_complete_callback):
    """
    إضافة رسالة إلى ذاكرة المستخدم المؤقتة مع مؤقت للتنفيذ بعد التأخير
    """
    if user_id not in buffers:
        buffers[user_id] = []

    buffers[user_id].append(message)

    if user_id in timers:
        timers[user_id].cancel()

    timers[user_id] = Timer(BUFFER_TIMEOUT, flush_buffer, args=(user_id, on_complete_callback))
    timers[user_id].start()

def flush_buffer(user_id, on_complete_callback):
    """
    تنفيذ الرد بعد تجميع الرسائل
    """
    messages = buffers.get(user_id, [])
    full_message = "\n".join(messages).strip()

    buffers.pop(user_id, None)
    timers.pop(user_id, None)

    if full_message:
        on_complete_callback(user_id, full_message)
