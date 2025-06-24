# bot_control.py

bot_status = {}

def is_bot_enabled(client_id):
    # القيمة الافتراضية: البوت مفعّل
    return bot_status.get(client_id, True)

def toggle_bot(client_id, command):
    command = command.strip().lower()
    if command == "ايقاف البوت":
        bot_status[client_id] = False
        return "✅ تم إيقاف البوت مؤقتًا لهذا العميل."
    elif command == "تشغيل البوت":
        bot_status[client_id] = True
        return "✅ تم تشغيل البوت مرة أخرى."
    return None
