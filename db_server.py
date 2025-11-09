# Standalone Mock Database Server.
#
# TCP server that listens on a dedicated port.
# Uses the Length-Prefixed Framing Protocol from common.protocol.
# All requests and responses are JSON strings.
# Persists data to local JSON files (simulating a DB) .
# Uses threading to handle multiple concurrent clients.
# Uses a lock to ensure file writes are thread-safe.

import socket
import threading
import json
import os
import sys
import logging

# Import our new protocol functions
try:
    from common import config
    from common.protocol import send_msg, recv_msg
except ImportError:
    print("Error: Could not import protocol.py.")
    print("Ensure 'common/protocol.py' exists and is in your Python path.")
    sys.exit(1)

# Server Configuration
DB_HOST = config.DB_HOST
DB_PORT = config.DB_PORT
STORAGE_DIR = 'storage'
USER_DB_FILE = os.path.join(STORAGE_DIR, 'users.json')
GAMELOG_DB_FILE = os.path.join(STORAGE_DIR, 'gamelogs.json')

# A single lock for all database file I/O 
db_lock = threading.Lock()

# Configure logging
logging.basicConfig(level=logging.INFO, format='[DB_SERVER] %(asctime)s - %(message)s')

# Database Helper Functions

def setup_storage():
    """Ensures the storage directory and initial DB files exist."""
    try:
        os.makedirs(STORAGE_DIR, exist_ok=True)
        
        # Initialize files if they don't exist
        with db_lock:
            if not os.path.exists(USER_DB_FILE):
                with open(USER_DB_FILE, 'w') as f:
                    json.dump({}, f)  # Start with an empty user object
            
            if not os.path.exists(GAMELOG_DB_FILE):
                with open(GAMELOG_DB_FILE, 'w') as f:
                    json.dump([], f) # Start with an empty list of logs
                    
    except OSError as e:
        logging.critical(f"Failed to create storage directory '{STORAGE_DIR}': {e}")
        sys.exit(1)

def load_db(filepath: str) -> dict | list:
    """Loads a JSON database file in a thread-safe way."""
    with db_lock:
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            logging.error(f"Error loading DB file '{filepath}': {e}")
            # Return a default empty structure if file is corrupt or empty
            return {} if filepath == USER_DB_FILE else []

def save_db(filepath: str, data: dict | list):
    """Saves data to a JSON database file in a thread-safe way."""
    with db_lock:
        try:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=4)
        except IOError as e:
            logging.error(f"Error saving DB file '{filepath}': {e}")

# Request Processing Logic

def process_request(request_data: dict) -> dict:
    # Main logic to handle a parsed JSON request.
    try:
        collection = request_data['collection']
        action = request_data['action']
        data = request_data.get('data', {})

        # === User Collection ===
        if collection == "User":
            users = load_db(USER_DB_FILE)
            
            if action == "create":  # Register
                username = data.get('username')
                password = data.get('password')
                if not username or not password:
                    return {"status": "error", "reason": "missing_fields"}
                
                if username in users:
                    return {"status": "error", "reason": "user_exists"} 
                
                users[username] = {"password": password, "status": "offline"}
                save_db(USER_DB_FILE, users)
                logging.info(f"Registered new user: {username}")
                return {"status": "ok"}

            elif action == "query": # Login
                username = data.get('username')
                password = data.get('password')
                if not username or not password:
                    return {"status": "error", "reason": "missing_fields"}
                user = users.get(username)
                if user and user['password'] == password:
                    logging.info(f"User login successful: {username}")
                    # Return user data (excluding password)
                    return {"status": "ok", "user": {"username": username}}
                else:
                    logging.warning(f"User login failed: {username}")
                    return {"status": "error", "reason": "invalid_credentials"} 
            
            elif action == "update": # For setting status
                username = data.get('username')
                new_status = data.get('status')
                
                if not username or not new_status:
                    return {"status": "error", "reason": "missing_fields_for_update"}
                
                if username not in users:
                    return {"status": "error", "reason": "user_not_found"}
                
                # Update the user's status in memory
                users[username]['status'] = new_status
                
                # Save the change back to the file
                save_db(USER_DB_FILE, users)
                logging.info(f"Updated status for {username} to {new_status}")
                return {"status": "ok"}
                
            else:
                return {"status": "error", "reason": f"Unknown action '{action}' for User"}

        # === GameLog Collection ===
        elif collection == "GameLog":
            logs = load_db(GAMELOG_DB_FILE)
            
            if action == "create": # Save a game result
                # 'data' should be the game log object 
                if not data:
                    return {"status": "error", "reason": "missing_gamelog_data"}
                logs.append(data)
                save_db(GAMELOG_DB_FILE, logs)
                logging.info(f"Saved new gamelog for match: {data.get('matchid')}")
                return {"status": "ok"}

            elif action == "query": # Get game logs (e.g., for a user)
                logging.info(f"Received query for GameLog with data: {data}")
                # TODO: add more complex queries
                user_id = data.get('userId')
                if not user_id:
                    return {"status": "ok", "logs": logs} # Return all logs
                
                user_logs = [log for log in logs if user_id in log.get('users', [])]
                return {"status": "ok", "logs": user_logs}


            else:
                return {"status": "error", "reason": f"Unknown action '{action}' for GameLog"}

        else:
            return {"status": "error", "reason": f"Unknown collection '{collection}'"}

    except KeyError as e:
        logging.warning(f"Request processing error: Missing key {e}")
        return {"status": "error", "reason": f"missing_key: {e}"}
    except Exception as e:
        logging.error(f"Unexpected error in process_request: {e}")
        return {"status": "error", "reason": "internal_server_error"}


# Client Handling Thread

def handle_client(client_socket: socket.socket, addr: tuple):
    """
    Runs in a separate thread for each connected client.
    Handles one request/response cycle per connection.
    """
    logging.info(f"Client connected from {addr}")
    response_data = {}
    
    try:
        # 1. Receive a message using our protocol
        request_bytes = recv_msg(client_socket)
        
        if request_bytes is None:
            logging.info(f"Client {addr} disconnected before sending data.")
            return

        # 2. Decode from bytes to string and parse JSON
        try:
            request_str = request_bytes.decode('utf-8')
            request_data = json.loads(request_str)
            logging.info(f"Received from {addr}: {request_data}")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logging.warning(f"Failed to decode/parse JSON from {addr}: {e}")
            response_data = {"status": "error", "reason": "invalid_json_format"}
            return # 'finally' block will send this response

        # 3. Process the request
        response_data = process_request(request_data)

    except socket.error as e:
        logging.warning(f"Socket error with client {addr}: {e}")
    except Exception as e:
        logging.error(f"Unhandled exception for client {addr}: {e}", exc_info=True)
        response_data = {"status": "error", "reason": "internal_server_error"}
        
    finally:
        # 4. Send the response
        try:
            if response_data: # Only send if we have a response
                response_bytes = json.dumps(response_data).encode('utf-8')
                send_msg(client_socket, response_bytes)
                logging.info(f"Sent to {addr}: {response_data}")
        except Exception as e:
            logging.error(f"Failed to send response to {addr}: {e}")
        
        # 5. Close the connection
        client_socket.close()
        logging.info(f"Connection closed for {addr}")

# Main Server Loop

def main():
    """Starts the DB server."""
    
    # 1. Ensure storage is ready
    setup_storage()
    
    # 2. Create the server socket
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        # 3. Bind and Listen
        server_socket.bind(('0.0.0.0', DB_PORT))
        server_socket.listen()
        logging.info(f"Database Server listening on {DB_HOST}:{DB_PORT}...")
        logging.info("Press Ctrl+C to stop.")

        # 4. Accept connections
        while True:
            try:
                # Wait for a client
                client_socket, addr = server_socket.accept()
                
                # Create and start a new thread to handle this client
                # Allows server to handle multiple clients at once
                client_thread = threading.Thread(
                    target=handle_client, 
                    args=(client_socket, addr)
                )
                client_thread.daemon = True # Allows server to exit even if threads are running
                client_thread.start()
                
            except socket.error as e:
                logging.error(f"Socket error while accepting connections: {e}")

    except KeyboardInterrupt:
        logging.info("Shutting down database server.")
    except Exception as e:
        logging.critical(f"A critical error occurred: {e}", exc_info=True)
    finally:
        server_socket.close()

if __name__ == "__main__":
    main()
