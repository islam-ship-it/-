import time

# البافر المؤقت لحفظ رسائل كل عميل
buffer_store = {}

# المدة اللي ننتظرها لتجميع الرسائل (بالثواني)
BUFFER_TIMEOUT = 3

def add_to_buffer(sender_id, message):
    current_time = time.time()
    buffer = buffer_store.get(sender_id, {"messages": [], "last_time": current_time})

    if current_time - buffer["last_time"] > BUFFER_TIMEOUT:
        # المدة انتهت، نرجّع الرسالة السابقة لو كانت موجودة
        if buffer["messages"]:
            full_message = " ".join(buffer["messages"])
            buffer_store[sender_id] = {"messages": [], "last_time": 0}
            return full_message
        else:
            buffer = {"messages": [], "last_time": current_time}

    buffer["messages"].append(message)
    buffer["last_time"] = current_time
    buffer_store[sender_id] = buffer

    # لو عدّى الوقت على آخر رسالة → نرجّع المجمّع
    if current_time - buffer["last_time"] >= BUFFER_TIMEOUT:
        full_message = " ".join(buffer["messages"])
        buffer_store[sender_id] = {"messages": [], "last_time": 0}
        return full_message

    return None
