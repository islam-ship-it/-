# bot_control.py

bot_status = {}

def is_bot_enabled(client_id):
    # الحالة الافتراضية: البوت شغّال
    return bot_status.get(client_id, True)

# alias للاستخدام في main.py
is_bot_active = is_bot_enabled

def toggle_bot(client_id, command):
    command = command.strip().lower()
    if command == "ايقاف البوت":
        bot_status[client_id] = False
        return "✅ تم إيقاف البوت مؤقتًا لهذا العميل."
    elif command == "تشغيل البوت":
        bot_status[client_id] = True
        return "✅ تم تفعيل البوت مرة أخرى."
    return None
