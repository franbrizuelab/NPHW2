import queue

g_lobby_send_queue = queue.Queue()

def send_to_lobby_queue(request: dict):
    """Puts a request into the lobby send queue."""
    g_lobby_send_queue.put(request)
