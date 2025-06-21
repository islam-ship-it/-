session_memory = {}

def get_session(user_id):
    return session_memory.get(user_id)

def save_session(user_id, data):
    session_memory[user_id] = data
