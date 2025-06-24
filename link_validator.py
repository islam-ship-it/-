def is_valid_service_link(text):
    if not text or not isinstance(text, str):
        return False

    common_keywords = [
        "facebook.com", "instagram.com", "tiktok.com", "youtube.com",
        "twitter.com", "linkedin.com", "snapchat.com", "kwai",
        "whatsapp.com", "telegram.me", "youtu.be"
    ]
    return any(domain in text for domain in common_keywords)
