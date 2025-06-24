def get_next_action(session, message):
    status = session.get("status", "idle")

    if status == "waiting_link":
        session["status"] = "waiting_payment"
        return "✅ تم استلام الرابط بنجاح.\nيرجى الآن تحويل المبلغ لإتمام الطلب."

    if status == "waiting_payment":
        return "📌 نحن في انتظار التحويل لإكمال تنفيذ طلبك."

    return None


def match_service(message, services, detected_count=None):
    message = message.lower()
    matched = []

    for service in services:
        platform = service["platform"].lower()
        stype = service["type"].lower()

        if platform in message or platform[:3] in message:
            if stype in message:
                if detected_count:
                    try:
                        if int(service["count"]) == int(detected_count):
                            matched.append(service)
                    except:
                        continue
                else:
                    matched.append(service)
    return matched

def apply_rules(message, intent, session, services, replies):
    contextual_response = get_next_action(session, message)
    if contextual_response:
        return contextual_response

    if intent == "ask_price":
        detected_count = session.get("detected_count")
        matched = match_service(message, services, detected_count)

        if matched:
            session["matched_services"] = matched
            responses = [
                f"💰 سعر {m['count']} {m['type']} على {m['platform']} = {m['price']} جنيه ({m['audience']})"
                for m in matched
            ]
            session["status"] = "waiting_link"
            return "\n".join(responses) + "\n\n📎 من فضلك ابعت لينك الخدمة دلوقتي علشان نبدأ."

        return "🔍 لم أتعرف على الخدمة أو العدد بدقة. من فضلك وضّح نوع الخدمة وعددها (مثال: 5000 متابع فيسبوك)."

    if intent == "confirm_payment":
        session["status"] = "completed"
        return "✅ تم تأكيد الدفع بنجاح. سيتم تنفيذ طلبك خلال أقرب وقت، وسنوافيك بالتحديثات."

    if intent == "followup":
        return replies.get("رد_ترحيبي", "👋 أهلاً بيك! تقدر تسأل عن أي خدمات أو اسعار اقدر اساعدك ازاي.")

    return replies.get("رد_افتراضي", "❓ من فضلك وضّح طلبك بشكل أوضح.")
