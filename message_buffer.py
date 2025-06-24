import time

# البافر المؤقت لحفظ رسائل كل عميل
buffer_store = {}

# المدة اللي ننتظرها لتجميع الرسائل (بالثواني)
BUFFER_TIMEOUT = 3

def add_to_buffer(sender_id, message):
    current_time = time.time()
    buffer = buffer_store.get(sender_id)

    if not buffer:
        # أول رسالة: نبدأ توقيت جديد
        buffer = {"messages": [], "start_time": current_time}

    # أضف الرسالة للبافر
    buffer["messages"].append(message)

    # لو عدى الوقت المسموح للتجميع، نرجع الرسائل كلها مرة واحدة
    if current_time - buffer["start_time"] >= BUFFER_TIMEOUT:
        full_message = " ".join(buffer["messages"])
        buffer_store[sender_id] = {"messages": [], "start_time": 0}
        return full_message

    buffer_store[sender_id] = buffer
    return None

def get_buffered_message(sender_id):
    buffer = buffer_store.get(sender_id)
    if buffer and buffer["messages"]:
        full_message = " ".join(buffer["messages"])
        buffer_store[sender_id] = {"messages": [], "start_time": 0}
        return full_message
    return None
