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
from datetime import datetime

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
GRAVITY_INTERVAL_MS = 400 # How often pieces fall (in ms)

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
                elif request.get("type") == "FORFEIT":
                    input_queue.put((player_id, "FORFEIT"))
                
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logging.warning(f"Invalid JSON from Player {player_id + 1}: {e}")
            
    except socket.error as e:
        logging.error(f"Socket error for Player {player_id + 1}: {e}")
        input_queue.put((player_id, "DISCONNECT"))
    finally:
        sock.close()
        logging.info(f"Client thread stopped for Player {player_id + 1}.")

# Game Logic

def broadcast_state(clients: list, game_p1: TetrisGame, game_p2: TetrisGame, remaining_time: int):
    """
    Builds the snapshot and sends it to both clients.
    """
    try:
        p1_state = game_p1.get_state_snapshot()
        p2_state = game_p2.get_state_snapshot()
        
        snapshot = {
            "type": "SNAPSHOT",
            "p1_state": p1_state,
            "p2_state": p2_state,
            "remaining_time": remaining_time
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

# UPDATE SIGNATURE
# rrrrr
def handle_game_end(clients: list, game_p1: TetrisGame, game_p2: TetrisGame, winner: str, reason: str, loser_username: str, p1_user: str, p2_user: str, room_id: int, start_time: float):
    """
    Handles all end-of-game logic:
    1. Builds the GameLog.
    2. Reports the log to the DB server.
    3. Sends the final GAME_OVER message to both clients.
    4. Notifies the lobby server that the game is over.
    """
    logging.info(f"Game loop finished. Winner: {winner}, Reason: {reason}")
    end_time = datetime.now()
    
    # 1. Build GameLog
    p1_results = {"userId": p1_user, "score": game_p1.score, "lines": game_p1.lines_cleared}
    p2_results = {"userId": p2_user, "score": game_p2.score, "lines": game_p2.lines_cleared}
    
    game_log = {
        "matchid": f"match_{int(time.time())}",
        "users": [p1_user, p2_user], # Use real usernames
        "results": [p1_results, p2_results],
        "winner": winner,
        "reason": reason,
        "start_time": datetime.fromtimestamp(start_time).isoformat(),
        "end_time": end_time.isoformat()
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
    winner_username = "TIE"
    if winner == "P1":
        winner_username = p1_user
    elif winner == "P2":
        winner_username = p2_user
        
    game_over_msg = {
        "type": "GAME_OVER",
        "winner": winner,
        "reason": reason,
        "loser_username": loser_username,
        "winner_username": winner_username,
        "p1_results": p1_results,
        "p2_results": p2_results,
        "room_id": room_id
    }

    try:
        for sock in list(clients):
            if sock:
                protocol.send_msg(sock, json.dumps(game_over_msg).encode('utf-8'))
    except Exception as e:
        logging.warning(f"Failed to send GAME_OVER message: {e}")



# Runs gravity, processes inputs, and broadcasts state
def game_loop(clients: list, input_queue: queue.Queue, game_p1: TetrisGame, game_p2: TetrisGame, p1_user: str, p2_user: str, room_id: int):
    logging.info("Game loop started for 'Lines Over Time' mode.")
    start_time = time.time()
    game_duration = 60  # GAME TIME VARIABLE
    winner = None

    last_gravity_tick_time = time.time()
    last_broadcast_time = 0

    while winner is None:
        current_time = time.time()
        elapsed_time = current_time - start_time

        # 1. Check for game end conditions
        if elapsed_time >= game_duration:
            break  # Time's up, exit loop to determine winner by lines

        if game_p1.game_over:
            winner = "P2"
            break
        if game_p2.game_over:
            winner = "P1"
            break

        # 2. Process Inputs
        try:
            while not input_queue.empty():
                player_id, action = input_queue.get_nowait()
                
                if action == "DISCONNECT" or action == "FORFEIT":
                    logging.info(f"Player {player_id + 1} disconnected or forfeited.")
                    winner = "P2" if player_id == 0 else "P1"
                    break

                if player_id == 0:
                    process_input(game_p1, action)
                elif player_id == 1:
                    process_input(game_p2, action)

        except queue.Empty:
            pass
        
        if winner:
            break

        # 3. Apply gravity (Tick) with correct timing
        if (current_time - last_gravity_tick_time) * 1000 >= GRAVITY_INTERVAL_MS:
            game_p1.tick()
            game_p2.tick()
            last_gravity_tick_time = current_time

        # 4. Broadcast State periodically
        if current_time - last_broadcast_time > 0.1: # Broadcast every 100ms
            remaining_time = max(0, int(game_duration - elapsed_time))
            broadcast_state(clients, game_p1, game_p2, remaining_time)
            last_broadcast_time = current_time
            
        time.sleep(0.01)

    # --- Loop has ended, determine the final winner ---
    reason = ""
    loser_username = None
    if winner is None:  # This means time ran out
        score_p1 = game_p1.score
        score_p2 = game_p2.score
        logging.info(f"Time's up! Final score P1:{score_p1} vs P2:{score_p2}")
        reason = "time_up"
        if score_p1 > score_p2:
            winner = "P1"
            loser_username = p2_user
        elif score_p2 > score_p1:
            winner = "P2"
            loser_username = p1_user
        else:
            winner = "TIE"
            reason = "tie"
    else:
        # This means someone topped out or forfeited
        if game_p1.game_over or game_p2.game_over:
            reason = "board_full"
            loser_username = p1_user if game_p1.game_over else p2_user
        else:
            reason = "forfeit"
            loser_username = p1_user if winner == "P2" else p2_user


    # Force both games to be "over" so the final board state is accurate
    game_p1.game_over = True
    game_p2.game_over = True

    handle_game_end(clients, game_p1, game_p2, winner, reason, loser_username, p1_user, p2_user, room_id, start_time)

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
    parser.add_argument('--p1', type=str, required=True, help='Username of Player 1')
    parser.add_argument('--p2', type=str, required=True, help='Username of Player 2')
    parser.add_argument('--room_id', type=int, required=True, help='ID of the room')
    args = parser.parse_args()
    PORT = args.port
    P1_USERNAME = args.p1
    P2_USERNAME = args.p2
    ROOM_ID = args.room_id
    
        # TODO: erase these temporary lines
    HOST = '0.0.0.0'
    game_seed = random.randint(0, 1_000_000)

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
        game_loop(clients, input_queue, game_p1, game_p2, P1_USERNAME, P2_USERNAME, ROOM_ID)

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