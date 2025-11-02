# Authoritative Game Server.
# Launched by the Lobby Server (or manually for testing).
# Waits for two clients to connect.
# Runs the game logic for both players.
# Broadcasts the game state (snapshots) to both clients.

import socket
import threading
import json
import sys
import os
import time
import random
import queue
import logging
import argparse

# Add project root to path
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from common import config
    from common import protocol
    from common.game_rules import TetrisGame
except ImportError:
    print("Error: Could not import common modules.")
    print("Ensure this file is in a folder next to the 'common' folder.")
    sys.exit(1)

# Configuration
HOST = config.LOBBY_HOST  # Bind to the same IP as the lobby
PORT = config.GAME_SERVER_START_PORT # This will be passed by the lobby
GRAVITY_INTERVAL_MS = 500 # How often pieces fall (in ms)

# Configure logging
logging.basicConfig(level=logging.INFO, format='[GAME_SERVER] %(asctime)s - %(message)s')

# Client Handler Thread

def handle_client(sock: socket.socket, player_id: int, input_queue: queue.Queue):
    """
    Runs in a thread for each client (P1 and P2).
    Listens for INPUT messages and puts them in the shared queue.
    """
    logging.info(f"Client thread started for Player {player_id + 1}.")
    try:
        while True:
            # Block waiting for a message
            data_bytes = protocol.recv_msg(sock)
            if data_bytes is None:
                logging.warning(f"Player {player_id + 1} disconnected.")
                input_queue.put((player_id, "DISCONNECT"))
                break
            
            try:
                request = json.loads(data_bytes.decode('utf-8'))
                if request.get("type") == "INPUT":
                    action = request.get("action")
                    if action:
                        # Put the input into the queue for the main loop
                        input_queue.put((player_id, action))
                
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logging.warning(f"Invalid JSON from Player {player_id + 1}: {e}")
            
    except socket.error as e:
        logging.error(f"Socket error for Player {player_id + 1}: {e}")
    finally:
        sock.close()
        # (TODO: Send a message to the queue to signal disconnect)
        logging.info(f"Client thread stopped for Player {player_id + 1}.")

# Game Logic

def broadcast_state(clients: list, game_p1: TetrisGame, game_p2: TetrisGame):
    """
    Builds the snapshot and sends it to both clients.
    """
    try:
        p1_state = game_p1.get_state_snapshot()
        p2_state = game_p2.get_state_snapshot()
        
        snapshot = {
            "type": "SNAPSHOT",
            "p1_state": p1_state,
            "p2_state": p2_state
        }
        
        json_bytes = json.dumps(snapshot).encode('utf-8')
        
        # Send the *same* snapshot to both clients
        for sock in clients:
            if sock:
                protocol.send_msg(sock, json_bytes)
                
    except socket.error as e:
        logging.warning(f"Failed to broadcast state: {e}. One client may have disconnected.")
    except Exception as e:
        logging.error(f"Error in broadcast_state: {e}", exc_info=True)

def process_input(game: TetrisGame, action: str):
    """Maps an action string to a game logic function."""
    if action == "MOVE_LEFT":
        game.move("left")
    elif action == "MOVE_RIGHT":
        game.move("right")
    elif action == "ROTATE":
        game.rotate()
    elif action == "SOFT_DROP":
        game.soft_drop()
    elif action == "HARD_DROP":
        game.hard_drop()

def handle_game_end(clients: list, game_p1: TetrisGame, game_p2: TetrisGame, winner: str):
    """
    Handles end-of-game logic:
    1. Builds the GameLog.
    2. Reports the log to the DB server.
    3. Sends the final GAME_OVER message to both clients.
    """
    logging.info(f"Game loop finished. Winner: {winner}")
    
    # 1. Build GameLog
    # (TODO: We should pass player *usernames* to the game server,
    # but for now, "Player1" and "Player2" are placeholders)
    p1_results = {"userId": "Player1", "score": game_p1.score, "lines": game_p1.lines_cleared}
    p2_results = {"userId": "Player2", "score": game_p2.score, "lines": game_p2.lines_cleared}
    
    game_log = {
        "matchid": f"match_{int(time.time())}",
        "users": ["Player1", "Player2"], # Placeholder usernames
        "results": [p1_results, p2_results],
        "winner": winner
    }
    
    # 2. Report to DB
    db_response = forward_to_db({
        "collection": "GameLog",
        "action": "create",
        "data": game_log
    })
    if db_response and db_response.get("status") == "ok":
        logging.info("GameLog saved to DB.")
    else:
        logging.warning(f"Failed to save GameLog to DB: {db_response}")

    # 3. Send final GAME_OVER message to clients
    game_over_msg = {
        "type": "GAME_OVER",
        "winner": winner,
        "p1_results": p1_results,
        "p2_results": p2_results
    }
    try:
        # Create a copy in case a client disconnected and the list changes
        for sock in list(clients):
            if sock:
                protocol.send_msg(sock, json.dumps(game_over_msg).encode('utf-8'))
    except Exception as e:
        logging.warning(f"Failed to send GAME_OVER message: {e}")


def game_loop(clients: list, input_queue: queue.Queue, game_p1: TetrisGame, game_p2: TetrisGame):
    """
    The main heartbeat of the server.
    Runs gravity, processes inputs, and broadcasts state.
    """
    logging.info("Game loop started.")
    last_tick_time = time.time()
    
    while True:
        # Process Inputs
        # Empty the input queue
        while not input_queue.empty():
            try:
                player_id, action = input_queue.get_nowait()
                
                if action == "DISCONNECT":
                    logging.info(f"Player {player_id + 1} disconnected. Ending game.")
                    winner = "P2" if player_id == 0 else "P1"
                    game_p1.game_over = True # Force end
                    game_p2.game_over = True # Force end
                    break # Exit input processing loop

                if player_id == 0:
                    process_input(game_p1, action)
                elif player_id == 1:
                    process_input(game_p2, action)
            except queue.Empty:
                break # Queue is empty, move on
            except Exception as e:
                logging.error(f"Error processing input: {e}")

        if winner:
            break
        # Run Gravity (Tick)
        current_time = time.time()
        if (current_time - last_tick_time) * 1000 >= GRAVITY_INTERVAL_MS:
            game_p1.tick()
            game_p2.tick()
            last_tick_time = current_time

            # Broadcast State
            # We broadcast *after* the tick
            broadcast_state(clients, game_p1, game_p2)

        # Check End Condition
        # (Simple survival mode)
        if game_p1.game_over or game_p2.game_over:
            logging.info("Game over condition met.")
            if winner is None: # Determine by disconnect
                if game_p1.game_over:
                    winner = "P2"
                elif game_p2.game_over:
                    winner = "P1"
                # TODO: tie logic
            break
            
        # Don't burn CPU
        time.sleep(0.01)
        
    handle_game_end(clients, game_p1, game_p2, winner)

def forward_to_db(request: dict) -> dict | None:
    """Acts as a client to the DB_Server."""
    try:
        # Use config for DB host/port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((config.DB_HOST, config.DB_PORT))
            request_bytes = json.dumps(request).encode('utf-8')
            protocol.send_msg(sock, request_bytes)
            response_bytes = protocol.recv_msg(sock)
            
            if response_bytes:
                return json.loads(response_bytes.decode('utf-8'))
            else:
                logging.warning("DB server closed connection unexpectedly.")
                return {"status": "error", "reason": "db_server_no_response"}
                
    except socket.error as e:
        logging.error(f"Failed to connect or communicate with DB server: {e}")
        return {"status": "error", "reason": f"db_server_connection_error: {e}"}

# Main Function

def main():
    parser = argparse.ArgumentParser(description="Tetris Game Server")
    parser.add_argument(
        '--port', 
        type=int, 
        default=config.GAME_SERVER_START_PORT, 
        help='Port to listen on'
    )
    args = parser.parse_args()
    PORT = args.port
    
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        server_socket.bind((HOST, PORT))
        server_socket.listen(2)
        logging.info(f"Game Server listening on {HOST}:{PORT}...")
    except Exception as e:
        logging.critical(f"Failed to bind socket: {e}")
        return

    clients = []
    client_threads = []
    input_queue = queue.Queue()

    try:
        # 1. Wait for exactly two clients
        while len(clients) < 2:
            logging.info(f"Waiting for {2 - len(clients)} more player(s)...")
            client_sock, addr = server_socket.accept()
            player_id = len(clients)
            
            clients.append(client_sock)
            logging.info(f"Player {player_id + 1} connected from {addr}.")
            
            role = "P1" if player_id == 0 else "P2"
            welcome_msg = {
                "type": "WELCOME",
                "role": role,
                "seed": game_seed # Send the seed here
            }
            try:
                protocol.send_msg(client_sock, json.dumps(welcome_msg).encode('utf-8'))
            except Exception as e:
                logging.error(f"Failed to send WELCOME message to {role}: {e}")
                # This client is bad, remove them and wait for a new one
                clients.pop()
                client_sock.close()
                continue
            
            # Start a thread to handle this client's inputs
            thread = threading.Thread(
                target=handle_client,
                args=(client_sock, player_id, input_queue),
                daemon=True
            )
            client_threads.append(thread)
            thread.start()

        logging.info("Two players connected. Starting game...")
        
        # 2. Create the game instances
        # Use the same seed for both players for identical piece sequences
        game_seed = random.randint(0, 1_000_000)
        game_p1 = TetrisGame(game_seed)
        game_p2 = TetrisGame(game_seed)
        
        # 3. Run the main game loop
        game_loop(clients, input_queue, game_p1, game_p2)

    except KeyboardInterrupt:
        logging.info("Shutting down game server.")
    except Exception as e:
        logging.error(f"Critical error in main: {e}", exc_info=True)
    finally:
        for sock in clients:
            sock.close()
        server_socket.close()
        logging.info("Game server shut down.")

if __name__ == "__main__":
    main()