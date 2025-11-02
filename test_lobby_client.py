# File: test_lobby_client.py
#
# A simple, interactive CLI client to test the lobby server.
# This script maintains a persistent connection and a login state.

import socket
import threading
import json
import sys
import os
import time

try:
    from common import config
    from common.protocol import send_msg, recv_msg
except ImportError:
    print("Error: Could not import common/config.py or common/protocol.py.")
    sys.exit(1)

# --- Client State ---
g_lobby_socket = None
g_running = True
g_username = None

# --- Helper Functions ---

def send_to_lobby(request: dict):
    """Sends a JSON request to the lobby server."""
    global g_lobby_socket, g_running
    if g_lobby_socket is None:
        print("[CLIENT] Not connected.")
        return
    try:
        json_bytes = json.dumps(request).encode('utf-8')
        send_msg(g_lobby_socket, json_bytes)
    except Exception as e:
        print(f"[CLIENT] Error sending message: {e}")
        g_running = False

def listen_thread_func(sock: socket.socket):
    """
    Runs in a background thread.
    Listens for any messages *from* the server.
    """
    global g_running
    while g_running:
        try:
            response_bytes = recv_msg(sock)
            if response_bytes is None:
                if g_running:
                    print("\n[SERVER] Server disconnected.")
                break
            
            response = json.loads(response_bytes.decode('utf-8'))
            print(f"\n[SERVER] {json.dumps(response, indent=2)}")
            
            # (Later, we'll handle 'invite' popups here)
            
        except (socket.error, json.JSONDecodeError, UnicodeDecodeError) as e:
            if g_running:
                print(f"\n[CLIENT] Error in listener thread: {e}")
            break
        except Exception as e:
            if g_running:
                print(f"\n[CLIENT] Unexpected listener error: {e}", exc_info=True)
            break
            
    g_running = False
    print("[CLIENT] Listener thread stopped. Press Enter to exit.")

def print_help():
    print("\n--- Lobby Test Client Commands ---")
    print("  help         - Show this menu")
    print("  register <u> <p> - Register a new user")
    print("  login <u> <p>    - Login as a user")
    print("  logout       - Logout")
    print("  who          - List online users")
    print("  rooms        - List available rooms")
    print("  create <name>  - Create a new room")
    print("  quit         - Exit the client")
    print("----------------------------------")

# --- Main Client Loop ---

def main():
    global g_lobby_socket, g_running, g_username
    
    # 1. Connect to Lobby
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((config.LOBBY_HOST, config.LOBBY_PORT)) # DB_HOST is '127.0.0.1'
        g_lobby_socket = sock
        print(f"Connected to Lobby at {config.DB_HOST}:{config.LOBBY_PORT}")
    except socket.error as e:
        print(f"Failed to connect to lobby: {e}")
        return

    # 2. Start listener thread
    listener = threading.Thread(target=listen_thread_func, args=(sock,), daemon=True)
    listener.start()

    print_help()

    # 3. Main input loop
    while g_running:
        try:
            cmd_line = input(f"({g_username or 'Not Logged In'}) > ").strip()
            if not cmd_line:
                continue
            
            parts = cmd_line.split()
            cmd = parts[0].lower()

            if cmd == 'quit':
                g_running = False
            
            elif cmd == 'help':
                print_help()
            
            # --- Actions ---
            elif cmd == 'register' and len(parts) == 3:
                send_to_lobby({"action": "register", "data": {"user": parts[1], "pass": parts[2]}})
            
            elif cmd == 'login' and len(parts) == 3:
                send_to_lobby({"action": "login", "data": {"user": parts[1], "pass": parts[2]}})
                # Note: We aren't tracking login state here, just sending
                # A real client would wait for the 'ok' response
                g_username = parts[1] # Simple state tracking
            
            elif cmd == 'logout':
                send_to_lobby({"action": "logout"})
                g_username = None
                g_running = False # Our server closes connection on logout
            
            elif cmd == 'who':
                send_to_lobby({"action": "list_users"})
            
            elif cmd == 'rooms':
                send_to_lobby({"action": "list_rooms"})
            
            elif cmd == 'create' and len(parts) >= 2:
                room_name = " ".join(parts[1:])
                send_to_lobby({"action": "create_room", "data": {"name": room_name}})
            
            else:
                print("Unknown command. Type 'help'.")
            
            time.sleep(0.1) # Give listener a chance to print

        except KeyboardInterrupt:
            g_running = False
        except EOFError:
            g_running = False

    # 4. Cleanup
    print("Shutting down...")
    if g_lobby_socket:
        g_lobby_socket.close()

if __name__ == "__main__":
    main()