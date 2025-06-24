import time

# البافر المؤقت لحفظ رسائل كل عميل
buffer_store = {}

# المدة اللي ننتظرها لتجميع الرسائل (بالثواني)
BUFFER_TIMEOUT = 3

def add_to_buffer(sender_id, message):
    current_time = time.time()
    buffer = buffer_store.get(sender_id, {"messages": [], "last_time": current_time})

    # إذا مر وقت طويل نبدأ من جديد
    if current_time - buffer["last_time"] > BUFFER_TIMEOUT:
        buffer = {"messages": [], "last_time": current_time}

    buffer["messages"].append(message)
    buffer["last_time"] = current_time
    buffer_store[sender_id] = buffer

    # لو عدى الوقت المحدد نرجع الرسائل المجمعة
    if len(buffer["messages"]) > 1 and current_time - buffer["last_time"] >= BUFFER_TIMEOUT:
        full_message = " ".join(buffer["messages"])
        buffer_store[sender_id] = {"messages": [], "last_time": 0}
        return full_message

    return None

def get_buffered_message(sender_id):
    buffer = buffer_store.get(sender_id)
    if buffer and buffer["messages"]:
        full_message = " ".join(buffer["messages"])
        buffer_store[sender_id] = {"messages": [], "last_time": 0}
        return full_message
    return None
