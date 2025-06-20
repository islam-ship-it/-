import time

session_memory = {}
MAX_SESSION_LENGTH = 20

def update_session_memory(user_id, role, content):
    if user_id not in session_memory:
        session_memory[user_id] = []

    session_memory[user_id].append({
        "role": role,
        "content": content,
        "timestamp": time.time()
    })

    if len(session_memory[user_id]) > MAX_SESSION_LENGTH:
        session_memory[user_id] = session_memory[user_id][-MAX_SESSION_LENGTH:]

def get_session_memory(user_id):
    return session_memory.get(user_id, [])
