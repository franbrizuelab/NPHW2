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
import subprocess 

# Import our protocol library
try:
    from common import config
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
# These store the LIVE state. The DB stores the PERSISTENT state.
# Locks to make these dictionaries thread-safe.

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

def find_free_port(start_port: int) -> int:
    """Finds an available TCP port, starting from start_port."""
    port = start_port
    while port < 65535:
        try:
            # Try to bind to the port
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('', port))
                # If bind succeeds, the port is free
                return port
        except OSError:
            # Port is already in use
            port += 1
    raise RuntimeError("Could not find a free port.")

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
            "data": {"username": username,"status": "online"}
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

    session_status = ""
    session_sock = None

    with g_session_lock:
        session = g_client_sessions.pop(username, None)
        if session:
            session_status = session.get("status", "online")
            session_sock = session.get("sock")
    
    if session:
        logging.info(f"User '{username}' logged out.")
        
        # --- NEW LOGIC ---
        # 1. Update DB status
        db_status_update_req = {
            "collection": "User", "action": "update",
            "data": {"username": username, "status": "offline"}
        }
        db_status_response = forward_to_db(db_status_update_req)
        if not db_status_response or db_status_response.get("status") != "ok":
            logging.warning(f"Failed to update 'offline' status in DB for {username}.")

        # 2. Check if user was in an "idle" room
        if session_status.startswith("in_room_"):
            try:
                room_id = int(session_status.split('_')[-1])
                with g_room_lock:
                    room = g_rooms.get(room_id)
                    if room:
                        # Only clean up if the room was IDLE.
                        # If "playing", the game server is in charge.
                        if room["status"] == "idle":
                            if username in room["players"]:
                                room["players"].remove(username)
                            
                            if not room["players"]:
                                del g_rooms[room_id]
                                logging.info(f"Room {room_id} is empty, deleting.")
                            elif room["host"] == username:
                                room["host"] = room["players"][0]
                                new_host = room["host"]
                                logging.info(f"Host {username} left idle room, promoting {new_host}.")
                                # (We could notify the new host here)
                        
            except (ValueError, IndexError):
                logging.warning(f"Could not parse room ID from status: {session_status}")
        
        # 3. Send final confirmation
        if session_sock:
            try:
                send_to_client(session_sock, {"status": "ok", "reason": "logout_successful"})
                session_sock.close()
            except Exception as e:
                logging.warning(f"Error during final logout send for {username}: {e}")

# Handles 'list_rooms' action.
def handle_list_rooms(client_sock: socket.socket):
    # This just gets the LIVE rooms from memory.
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
    
    # 1. Check if user is already in another room
    with g_session_lock:
        session = g_client_sessions.get(username)
        if not session:
            send_to_client(client_sock, {"status": "error", "reason": "session_not_found"})
            return
        
        if session["status"] != "online":
            send_to_client(client_sock, {"status": "error", "reason": "already_in_a_room"})
            return
    
    room_id = -1
    with g_room_lock:
        # 2. Create a new room
        room_id = g_room_counter
        g_room_counter += 1
        
        g_rooms[room_id] = {
            "name": room_name,
            "host": username,
            "players": [username],
            "status": "idle"
        }
    
    # 3. Update the user's status
    with g_session_lock:
        g_client_sessions[username]["status"] = f"in_room_{room_id}"
        
    logging.info(f"User '{username}' created room {room_id} ('{room_name}').")
    
    # 4. Send the new room data back to the client
    room_update_msg = {
        "type": "ROOM_UPDATE",
        "room_id": room_id,
        "name": room_name,
        "players": [username], # Creator is the only one in it
        "host": username
    }
    send_to_client(client_sock, room_update_msg)

def handle_join_room(client_sock: socket.socket, username: str, data: dict):
    """Handles 'join_room' action."""
    global g_room_lock, g_rooms, g_client_sessions
    
    try:
        room_id = int(data.get("room_id"))
    except (TypeError, ValueError):
        send_to_client(client_sock, {"status": "error", "reason": "invalid_room_id"})
        return

    # 1. Check if user is already in a room
    with g_session_lock:
        session = g_client_sessions.get(username)
        if session and session["status"] != "online":
            send_to_client(client_sock, {"status": "error", "reason": "already_in_a_room"})
            return
            
    # 2. Find and validate the room
    all_players_in_room = []
    with g_room_lock:
        room = g_rooms.get(room_id)
        
        if not room:
            send_to_client(client_sock, {"status": "error", "reason": "room_not_found"})
            return
        
        if room["status"] != "idle":
            send_to_client(client_sock, {"status": "error", "reason": "room_is_playing"})
            return
            
        if len(room["players"]) >= 2:
            send_to_client(client_sock, {"status": "error", "reason": "room_is_full"})
            return
            
        # 3. Join the room
        room["players"].append(username)
        all_players_in_room = list(room["players"]) # Get a copy of the player list

    # 4. Update user's session status
    with g_session_lock:
        g_client_sessions[username]["status"] = f"in_room_{room_id}"
        
    logging.info(f"User '{username}' joined room {room_id}.")

    # 5. Notify all players in the room of the change
    room_update_msg = {
        "type": "ROOM_UPDATE",
        "room_id": room_id,
        "players": all_players_in_room,
        "host": room.get("host")
    }
    
    with g_session_lock:
        for player_name in all_players_in_room:
            player_session = g_client_sessions.get(player_name)
            if player_session:
                send_to_client(player_session["sock"], room_update_msg)

def handle_leave_room(username: str):
    """Handles a user leaving a room."""
    room_id = None
    with g_session_lock:
        session = g_client_sessions.get(username)
        if session and session["status"].startswith("in_room_"):
            try:
                room_id = int(session["status"].split('_')[-1])
            except (ValueError, IndexError):
                pass

    if room_id is None:
        return # User is not in a room

    with g_room_lock:
        room = g_rooms.get(room_id)
        if not room:
            return # Room doesn't exist

        if username in room["players"]:
            room["players"].remove(username)

        # If the host leaves, or the room becomes empty, delete it
        if room["host"] == username or not room["players"]:
            remaining_players = list(room["players"]) # Make a copy
            logging.info(f"Host {username} is leaving room {room_id}. Notifying {remaining_players}.")

            # Notify remaining players FIRST, before deleting the room
            kick_msg = {"type": "KICKED_FROM_ROOM", "reason": "The host has left the room."}
            with g_session_lock:
                for player_name in remaining_players:
                    player_session = g_client_sessions.get(player_name)
                    if player_session:
                        send_to_client(player_session["sock"], kick_msg)
                        player_session["status"] = "online"
            

            time.sleep(1) # Let's see if the other user leaves the room
            # THEN, delete the room
            del g_rooms[room_id]
            logging.info(f"Room {room_id} closed.")
        else:
            # Notify remaining players of the updated room state
            room_update_msg = {
                "type": "ROOM_UPDATE",
                "room_id": room_id,
                "players": room["players"],
                "host": room.get("host")
            }
            with g_session_lock:
                for player_name in room["players"]:
                    player_session = g_client_sessions.get(player_name)
                    if player_session:
                        send_to_client(player_session["sock"], room_update_msg)

    with g_session_lock:
        session = g_client_sessions.get(username)
        if session:
            session["status"] = "online"

def handle_start_game(client_sock: socket.socket, username: str):
    """
    Handles 'start_game' action.
    - User must be the host of a full room.
    - Launches a new game_server.py process.
    - Notifies both players of the game server's address.
    """
    global g_room_lock, g_rooms, g_client_sessions
    
    room_id = None
    player1_name = None
    player2_name = None
    p1_sock = None
    p2_sock = None
    
    # 1. Lock g_session_lock *first* to check user status
    with g_session_lock:
        status = g_client_sessions.get(username, {}).get("status", "")
        if status.startswith("in_room_"):
            try:
                room_id = int(status.split('_')[-1])
            except (ValueError, IndexError): pass
        
        if room_id is None:
            send_to_client(client_sock, {"status": "error", "reason": "not_in_a_room"})
            return

        # 2. Lock g_room_lock to check room status
        with g_room_lock:
            room = g_rooms.get(room_id)
            if room is None:
                send_to_client(client_sock, {"status": "error", "reason": "room_not_found"})
                return
            if room["host"] != username:
                send_to_client(client_sock, {"status": "error", "reason": "not_room_host"})
                return
            if len(room["players"]) != 2:
                send_to_client(client_sock, {"status": "error", "reason": "room_not_full"})
                return
            
            # All checks passed. Set status to "playing" immediately.
            room["status"] = "playing"
            
            player1_name = room["players"][0]
            player2_name = room["players"][1]
            
            # Update both players' session status
            p1_session = g_client_sessions.get(player1_name)
            p2_session = g_client_sessions.get(player2_name)
            
            if p1_session: 
                p1_session["status"] = "playing"
                p1_sock = p1_session["sock"]
            if p2_session:
                p2_session["status"] = "playing"
                p2_sock = p2_session["sock"]
    
    # 3. All locks are released. Now launch the game and notify.
    try:
        game_port = find_free_port(config.GAME_SERVER_START_PORT)
        
        command = [
            "python3", "game_server.py", 
            "--port", str(game_port),
            "--p1", player1_name,
            "--p2", player2_name,
            "--room_id", str(room_id) # Add room_id to the command
        ]
        subprocess.Popen(command)
        
        logging.info(f"Launched GameServer for {player1_name} and {player2_name} on port {game_port}")
        
        time.sleep(0.5) # Sleep before telling clients

        game_info_msg = {
            "type": "GAME_START",
            "host": config.LOBBY_HOST,
            "port": game_port
        }
        
        # Notify both players
        if p1_sock: send_to_client(p1_sock, game_info_msg)
        if p2_sock: send_to_client(p2_sock, game_info_msg)
            
    except Exception as e:
        logging.error(f"Failed to start game for room {room_id}: {e}")
        # (TODO: Rollback state to "idle" if this fails)

def handle_invite(client_sock: socket.socket, inviter_username: str, data: dict):
    """Handles 'invite' action."""
    target_username = data.get("target_user")
    if not target_username:
        send_to_client(client_sock, {"status": "error", "reason": "no_target_user"})
        return
        
    if target_username == inviter_username:
        send_to_client(client_sock, {"status": "error", "reason": "cannot_invite_self"})
        return

    room_id = None
    target_sock = None
    
    with g_session_lock:
        # 1. Get inviter's room
        inviter_session = g_client_sessions.get(inviter_username)
        if inviter_session and inviter_session["status"].startswith("in_room_"):
            try:
                room_id = int(inviter_session["status"].split('_')[-1])
            except (ValueError, IndexError):
                pass # room_id remains None
        
        if room_id is None:
            send_to_client(client_sock, {"status": "error", "reason": "not_in_a_room"})
            return
            
        # 2. Find target user and check their status
        target_session = g_client_sessions.get(target_username)
        if not target_session:
            send_to_client(client_sock, {"status": "error", "reason": "user_not_online"})
            return
            
        if target_session["status"] != "online":
            send_to_client(client_sock, {"status": "error", "reason": "user_is_busy"})
            return
        
        target_sock = target_session["sock"]

    # 3. Send the invite
    if target_sock:
        invite_msg = {
            "type": "INVITE_RECEIVED",
            "from_user": inviter_username,
            "room_id": room_id
        }
        send_to_client(target_sock, invite_msg)
        send_to_client(client_sock, {"status": "ok", "reason": "invite_sent"})
        logging.info(f"User '{inviter_username}' invited '{target_username}' to room {room_id}.")
    else:
        # This case should be rare but good to handle
        send_to_client(client_sock, {"status": "error", "reason": "could_not_find_target_socket"})

# rrrrr
def handle_game_over(room_id: int):
    """
    Deletes a room and sets its players to online status after a game.
    """
    with g_session_lock, g_room_lock:
        room = g_rooms.get(room_id)
        if not room or room["status"] != "playing":
            return # Nothing to do

        logging.info(f"Game over for room {room_id}. Deleting room.")
        player_list = list(room["players"]) # Copy for safe iteration
        del g_rooms[room_id]

        # Update player statuses
        for username in player_list:
            session = g_client_sessions.get(username)
            if session:
                session["status"] = "online" # Back to being online

        # Broadcast the changes to all clients
        public_rooms = []
        for room_id, room_data in g_rooms.items():
            if room_data["status"] == "idle": # Only show idle rooms
                public_rooms.append({
                    "id": room_id,
                    "name": room_data["name"],
                    "host": room_data["host"],
                    "players": len(room_data["players"])
                })
        user_list = []
        user_list = [
            {"username": user, "status": data["status"]}
            for user, data in g_client_sessions.items()
        ]

        for session in g_client_sessions.values():
            send_to_client(session["sock"], {"status": "ok", "rooms": public_rooms})
            send_to_client(session["sock"], {"status": "ok", "users": user_list})

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
                # Client disconnected (or network error)
                logging.info(f"Client {addr} disconnected.")
                break
                
            # 2. Parse the message
            try:
                request_str = request_bytes.decode('utf-8')
                request = json.loads(request_str)
                logging.info(f"rx: {request}")
                action = request.get('action')
                data = request.get('data', {})
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                logging.warning(f"Invalid JSON from {addr}: {e}")
                send_to_client(client_sock, {"status": "error", "reason": "invalid_json_format"})
                continue

            # 3. Process the action
            
            # Actions allowed BEFORE login
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
            
            # Actions allowed AFTER login
            else:
                if action == 'logout':
                    break # Break the loop, 'finally' will clean up
                
                elif action == 'list_rooms':
                    handle_list_rooms(client_sock)
                
                elif action == 'list_users':
                    handle_list_users(client_sock)
                
                elif action == 'create_room':
                    handle_create_room(client_sock, username, data)
                
                elif action == 'start_game':
                    handle_start_game(client_sock, username)
                
                elif action == 'join_room':
                    handle_join_room(client_sock, username, data)

                elif action == 'leave_room':
                    # logging.info(f"{username} leaving room via action")
                    handle_leave_room(username)
                
                elif action == 'invite':
                    handle_invite(client_sock, username, data)
                
                elif action == 'game_over':
                    room_id = data.get('room_id')
                    if room_id is not None:
                        handle_game_over(room_id)

                elif action == 'query_gamelogs':
                    logging.info(f"Received query_gamelogs request from {username}")
                    db_request = {
                        "collection": "GameLog",
                        "action": "query",
                        "data": data
                    }
                    db_response = forward_to_db(db_request)
                    if db_response and db_response.get("status") == "ok":
                        send_to_client(client_sock, {"type": "gamelog_response", "logs": db_response.get("logs", [])})
                    else:
                        send_to_client(client_sock, {"status": "error", "reason": "failed_to_fetch_gamelogs"})


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
    
    # Initialize the socket for the server
    # AF_INET: use IPv4 adress
    # SOCK_STREAM: TCP socket
    # SO_REUSEADDR: Allows to reuse server addresss after it has been closed
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