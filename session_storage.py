import os
import json

SESSIONS_FOLDER = 'sessions'
os.makedirs(SESSIONS_FOLDER, exist_ok=True)

def get_session(user_id):
    filepath = os.path.join(SESSIONS_FOLDER, f"{user_id}.json")
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"history": [], "thread_id": None}

def save_session(user_id, session_data):
    filepath = os.path.join(SESSIONS_FOLDER, f"{user_id}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)
