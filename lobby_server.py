# Central Lobby Server.
# TCP server that listens on a dedicated port
# Handles client connections in separate threads.
# Manages user state (login, logout) and room state.
# Acts as a CLIENT to 'db_server.py' for persistent data.
# Uses the Length-Prefixed Framing Protocol from common.protocol.

import socket
import threading
import json
import sys
import logging
import os
import time
import subprocess # Will be needed later for launching game_server.py

# Import our protocol library
try:
    from common.protocol import send_msg, recv_msg
except ImportError:
    print("Error: Could not import protocol.py.")
    print("Ensure 'common/protocol.py' exists and is in your Python path.")
    sys.exit(1)

# Server Configuration
LOBBY_HOST = config.LOBBY_HOST
LOBBY_PORT = config.LOBBY_PORT
DB_HOST = config.DB_HOST
DB_PORT = config.DB_PORT

# Configure logging
logging.basicConfig(level=logging.INFO, format='[LOBBY_SERVER] %(asctime)s - %(message)s')

# Global State
# These store the *live* state. The DB stores the *persistent* state.
# We need locks to make these dictionaries thread-safe.

# g_client_sessions: maps {username: {"sock": socket, "addr": tuple, "status": "online" | "in_room"}}
g_client_sessions = {}
g_session_lock = threading.Lock()

# g_rooms: maps {room_id: {"name": str, "host": str, "players": [list_of_usernames], "status": "idle"}}
g_rooms = {}
g_room_lock = threading.Lock()
g_room_counter = 100 # Simple room ID counter

# DB Helper Function

def forward_to_db(request: dict) -> dict | None:
    """
    Acts as a client to the DB_Server.
    Opens a new connection, sends one request, gets one response.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((DB_HOST, DB_PORT))
            
            # 1. Send request
            request_bytes = json.dumps(request).encode('utf-8')
            send_msg(sock, request_bytes)
            
            # 2. Receive response
            response_bytes = recv_msg(sock)
            
            if response_bytes:
                return json.loads(response_bytes.decode('utf-8'))
            else:
                logging.warning("DB server closed connection unexpectedly.")
                return {"status": "error", "reason": "db_server_no_response"}
                
    except socket.error as e:
        logging.error(f"Failed to connect or communicate with DB server: {e}")
        return {"status": "error", "reason": f"db_server_connection_error: {e}"}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logging.error(f"Failed to decode DB server response: {e}")
        return {"status": "error", "reason": "db_server_bad_response"}

# Client Helper Function

def send_to_client(client_sock: socket.socket, response: dict):
    """Encodes and sends a JSON response to a client."""
    try:
        response_bytes = json.dumps(response).encode('utf-8')
        send_msg(client_sock, response_bytes)
    except Exception as e:
        logging.warning(f"Failed to send message to client: {e}")

# Request Handlers

def handle_register(client_sock: socket.socket, data: dict) -> dict:
    """Handles 'register' action."""
    username = data.get('user')
    password = data.get('pass')
    
    if not username or not password:
        return {"status": "error", "reason": "missing_fields"}

    # Forward to DB server
    db_request = {
        "collection": "User",
        "action": "create",
        "data": {
            "username": username,
            "password": password
        }
    }
    db_response = forward_to_db(db_request)
    
    # Pass the DB response directly back to the client
    return db_response

def handle_login(client_sock: socket.socket, addr: tuple, data: dict) -> str | None:
    """
    Handles 'login' action.
    If successful, adds user to g_client_sessions and returns username.
    If failed, returns None.
    """
    username = data.get('user')
    password = data.get('pass')
    
    if not username or not password:
        send_to_client(client_sock, {"status": "error", "reason": "missing_fields"})
        return None

    # Check if already logged in
    with g_session_lock:
        if username in g_client_sessions:
            send_to_client(client_sock, {"status": "error", "reason": "already_logged_in"})
            return None

    # Forward to DB server to validate
    db_request = {
        "collection": "User",
        "action": "query",
        "data": {
            "username": username,
            "password": password
        }
    }
    db_response = forward_to_db(db_request)
    
    if db_response and db_response.get("status") == "ok":
        # Login successful!
        logging.info(f"User '{username}' logged in from {addr}.")
        
        # Add to our live session tracking
        with g_session_lock:
            g_client_sessions[username] = {
                "sock": client_sock,
                "addr": addr,
                "status": "online"
            }
        
        db_status_update_req = {
            "collection": "User",
            "action": "update",
            "data": {
                "username": username,
                "status": "online"
            }
        }
        db_status_response = forward_to_db(db_status_update_req)
        if not db_status_response or db_status_response.get("status") != "ok":
            # Log a warning, but don't fail the login
            logging.warning(f"Failed to update 'online' status in DB for {username}.")

        # Send success to client
        send_to_client(client_sock, {"status": "ok", "reason": "login_successful"})
        
        return username
    else:
        # Login failed
        logging.warning(f"Failed login attempt for '{username}'.")
        reason = db_response.get("reason", "invalid_credentials")
        send_to_client(client_sock, {"status": "error", "reason": reason})
        return None

def handle_logout(username: str):
    """
    Handles 'logout' action or clean-up on disconnect.
    """
    if not username:
        return

    with g_session_lock:
        session = g_client_sessions.pop(username, None)
    
    if session:
        logging.info(f"User '{username}' logged out.")
        
        db_status_update_req = {
            "collection": "User",
            "action": "update",
            "data": {
                "username": username,
                "status": "offline"
            }
        }
        db_status_response = forward_to_db(db_status_update_req)
        if not db_status_response or db_status_response.get("status") != "ok":
            logging.warning(f"Failed to update 'offline' status in DB for {username}.")
        # (TODO: Remove user from any room they were in)
        
        # Send final confirmation and close socket
        try:
            send_to_client(session["sock"], {"status": "ok", "reason": "logout_successful"})
            session["sock"].close()
        except Exception as e:
            logging.warning(f"Error during final logout send for {username}: {e}")

def handle_list_rooms(client_sock: socket.socket):
    """Handles 'list_rooms' action."""
    # This just gets the *live* rooms from memory.
    # (A better version might query the DB for public/persistent rooms)
    
    public_rooms = []
    with g_room_lock:
        for room_id, room_data in g_rooms.items():
            if room_data["status"] == "idle": # Only show idle rooms
                public_rooms.append({
                    "id": room_id,
                    "name": room_data["name"],
                    "host": room_data["host"],
                    "players": len(room_data["players"])
                })
                
    send_to_client(client_sock, {"status": "ok", "rooms": public_rooms})

def handle_list_users(client_sock: socket.socket):
    """Handles 'list_users' action."""
    # This just gets the *live* users from memory.
    with g_session_lock:
        # Get all usernames and their status
        user_list = [
            {"username": user, "status": data["status"]}
            for user, data in g_client_sessions.items()
        ]
        
    send_to_client(client_sock, {"status": "ok", "users": user_list})

def handle_create_room(client_sock: socket.socket, username: str, data: dict):
    """Handles 'create_room' action."""
    global g_room_counter
    room_name = data.get("name", f"{username}'s Room")
    
    with g_room_lock:
        # (TODO: Check if user is already in another room)
        
        # Create a new room
        room_id = g_room_counter
        g_room_counter += 1
        
        g_rooms[room_id] = {
            "name": room_name,
            "host": username,
            "players": [username],
            "status": "idle"
        }
    
    with g_session_lock:
        g_client_sessions[username]["status"] = f"in_room_{room_id}"
        
    logging.info(f"User '{username}' created room {room_id} ('{room_name}').")
    send_to_client(client_sock, {"status": "ok", "room_id": room_id, "name": room_name})

# Client Handling Thread

def handle_client(client_sock: socket.socket, addr: tuple):
    """
    Runs in a separate thread for each connected client.
    Manages the client's session from login to logout.
    """
    logging.info(f"Client connected from {addr}")
    username = None # Tracks the logged-in user for this thread
    
    try:
        while True:
            # 1. Receive a message
            request_bytes = recv_msg(client_sock)
            if request_bytes is None:
                # Client disconnected gracefully (or network error)
                logging.info(f"Client {addr} disconnected.")
                break
                
            # 2. Parse the message
            try:
                request_str = request_bytes.decode('utf-8')
                request = json.loads(request_str)
                action = request.get('action')
                data = request.get('data', {})
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                logging.warning(f"Invalid JSON from {addr}: {e}")
                send_to_client(client_sock, {"status": "error", "reason": "invalid_json_format"})
                continue

            # 3. Process the action
            
            # Actions allowed *before* login
            if username is None:
                if action == 'register':
                    response = handle_register(client_sock, data)
                    send_to_client(client_sock, response)
                
                elif action == 'login':
                    # handle_login sends its own responses
                    username = handle_login(client_sock, addr, data)
                
                elif action == 'logout':
                    break # Just close the connection
                
                else:
                    send_to_client(client_sock, {"status": "error", "reason": "must_be_logged_in"})
            
            # Actions allowed *after* login
            else:
                if action == 'logout':
                    break # Break the loop, 'finally' will clean up
                
                elif action == 'list_rooms':
                    handle_list_rooms(client_sock)
                
                elif action == 'list_users':
                    handle_list_users(client_sock)
                
                elif action == 'create_room':
                    handle_create_room(client_sock, username, data)
                
                # (TODO: Add 'join_room', 'invite', 'start_game')
                
                else:
                    send_to_client(client_sock, {"status": "error", "reason": f"unknown_action: {action}"})

    except Exception as e:
        logging.error(f"Unhandled exception for {addr} (user: {username}): {e}", exc_info=True)
        
    finally:
        # Clean-up
        # Ensure user is logged out and socket is closed
        if username:
            handle_logout(username)
        else:
            # If they never logged in, just close the socket
            client_sock.close()
            
        logging.info(f"Connection closed for {addr} (user: {username})")

# Main Server Loop

def main():
    """Starts the Lobby server."""
    
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        server_socket.bind((LOBBY_HOST, LOBBY_PORT))
        server_socket.listen()
        logging.info(f"Lobby Server listening on {LOBBY_HOST}:{LOBBY_PORT}...")
        logging.info("Press Ctrl+C to stop.")

        while True:
            try:
                client_socket, addr = server_socket.accept()
                
                # Start a new thread for each client
                client_thread = threading.Thread(
                    target=handle_client, 
                    args=(client_socket, addr)
                )
                client_thread.daemon = True
                client_thread.start()
                
            except socket.error as e:
                logging.error(f"Socket error while accepting connections: {e}")

    except KeyboardInterrupt:
        logging.info("Shutting down lobby server.")
    except Exception as e:
        logging.critical(f"A critical error occurred: {e}", exc_info=True)
    finally:
        server_socket.close()

if __name__ == "__main__":
    main()