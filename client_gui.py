# File: client_gui.py
#
# This is the FULL client, which handles both Lobby and Game.
# A state machine: LOGIN -> LOBBY -> IN_ROOM -> GAME
# Connects to the Lobby Server first.
# Receives a "hand-off" to connect to the Game Server.

import pygame
import socket
import threading
import json
import sys
import os
import time
import logging
import queue
import select
import random
import records_screen
from shared import g_lobby_send_queue, send_to_lobby_queue

# Add project root to path
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from common import config
    from common import protocol
    from common.game_rules import PIECE_SHAPES
except ImportError:
    print("Error: Could not import common/protocol.py or common/config.py.")
    sys.exit(1)

# Logging
# logging.basicConfig(level=logging.INFO, format='[CLIENT_GUI] %(asctime)s - %(message)s')
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

# Customization Configuration
CONFIG = {
    "TIMING": {"FPS": 30},
    "SCREEN": {"WIDTH": 900, "HEIGHT": 700},
    "SIZES": {"BLOCK_SIZE": 30, "SMALL_BLOCK_SIZE": 15},
    "STYLE": {"CORNER_RADIUS": 5,
              "LOBBY_BOX_BORDER_COLOR": (255, 255, 255), # White
              "LOBBY_BOX_BORDER_WIDTH": 1
             },
    "COLORS": {
        "BACKGROUND": (20, 20, 30),
        "GRID_LINES": (40, 40, 50),
        "TEXT": (255, 255, 255),
        "GAME_OVER": (255, 0, 0),
        "BUTTON": (70, 70, 90),
        "BUTTON_HOVER": (100, 100, 120),
        "INPUT_BOX": (10, 10, 20),
        "INPUT_TEXT": (200, 200, 200),
        "INPUT_ACTIVE": (50, 50, 70),
        "ERROR": (200, 50, 50),
        "PIECE_COLORS": [
            (0, 0, 0),
            (1, 237, 250),  # Cyan
            (254, 251, 52),  # Yellow
            (128, 0, 128),  # Purple
            (0, 119, 211),    # BLue
            (255, 165, 0),  # Orange
            (57, 137, 47),     # Green
            (253, 63, 89)  # Salmon (reddish)
        ]
    },
    "POSITIONS": {
        "MY_BOARD": (50, 50), "OPPONENT_BOARD": (640, 100),
        "MY_SCORE":       (370, 50),
        "MY_LINES":       (370, 80),
        "TIME":           (370, 110),
        "NEXT_PIECE":     (370, 150),
        "OPPONENT_SCORE": (640, 50),
        "OPPONENT_LINES": (600, 75), 
        "GAME_OVER_TEXT": (100, 300),
        "USERS_BOX_RECT": (440, 190, 370, 420) # X, Y, Width, Height
    },
    "FONTS": {
        "DEFAULT_FONT": 'assets/fonts/PressStart2P-Regular.ttf',
        "SIZES": {
            "TINY": 10,
            "SMALL": 15,
            "MEDIUM": 20,
            "LARGE": 25,
            "TITLE": 36,
            "GAME_OVER": 40
        },
        "OBJECTS": {
            "DEFAULT": None,
            "TINY": None,
            "SMALL": None,
            "MEDIUM": None,
            "LARGE": None,
            "TITLE": None,
            "GAME_OVER": None
        }
    },
    "NETWORK": {
        "HOST": config.LOBBY_HOST,
        "PORT": config.LOBBY_PORT
    },
    "BACKGROUND_ANIMATION": {
        "NUM_PIECES": 20,
        "MIN_SIZE": 10,
        "MAX_SIZE": 50,
        "MIN_SPEED": 0.5,
        "MAX_SPEED": 4.0,
        "ALPHA": 50 # 0-255
    }
}

# --- Block Rendering Logic ---
g_gradient_cache = {}

def get_gradient_block(size, color):
    corner_radius = CONFIG["STYLE"]["CORNER_RADIUS"]
    cache_key = (size[0], size[1], color[0], color[1], color[2], corner_radius)
    if cache_key in g_gradient_cache:
        return g_gradient_cache[cache_key]

    block_surface = pygame.Surface(size, pygame.SRCALPHA)
    pygame.draw.rect(block_surface, (255, 255, 255), block_surface.get_rect(), 0, border_radius=corner_radius)
    
    border_width = 1
    inset_rect = block_surface.get_rect().inflate(-border_width * 2, -border_width * 2)

    dark_color = tuple(c * 0.8 for c in color)
    light_color = (220, 220, 220)
    
    for y in range(inset_rect.height):
        for x in range(inset_rect.width):
            factor = ((x / inset_rect.width) + (y / inset_rect.height)) / 2.5
            pixel_color = tuple(
                int(start + (end - start) * factor)
                for start, end in zip(dark_color, light_color)
            )
            block_surface.set_at((inset_rect.left + x, inset_rect.top + y), pixel_color)

    g_gradient_cache[cache_key] = block_surface
    return block_surface

# UI helper classes
class TextInput:
    def __init__(self, x, y, w, h, font, text=''):
        self.rect = pygame.Rect(x, y, w, h)
        self.color = CONFIG["COLORS"]["INPUT_BOX"]
        self.text = text
        self.font = font
        self.active = False
        self.text_surface = self.font.render(text, True, self.color)
    
    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.active = self.rect.collidepoint(event.pos)
            self.color = CONFIG["COLORS"]["INPUT_ACTIVE"] if self.active else CONFIG["COLORS"]["INPUT_BOX"]
        if event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_RETURN:
                return "enter"
            elif event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            else:
                self.text += event.unicode
            self.text_surface = self.font.render(self.text, True, CONFIG["COLORS"]["INPUT_TEXT"])
    
    def draw(self, screen):
        pygame.draw.rect(screen, self.color, self.rect, 0, border_radius=CONFIG["STYLE"]["CORNER_RADIUS"])
        pygame.draw.rect(screen, CONFIG["COLORS"]["TEXT"], self.rect, 1, border_radius=CONFIG["STYLE"]["CORNER_RADIUS"]) # Thin border
        screen.blit(self.text_surface, (self.rect.x + 5, self.rect.y + 5))

class Button:
    def __init__(self, x, y, w, h, font, text=''):
        self.rect = pygame.Rect(x, y, w, h)
        self.color = CONFIG["COLORS"]["BUTTON"]
        self.text = text
        self.font = font
        self.is_focused = False
    
    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            if self.rect.collidepoint(event.pos):
                return True
        return False
        
    def draw(self, screen, blink_on=True):
        color = self.color
        if self.rect.collidepoint(pygame.mouse.get_pos()) or self.is_focused:
            color = CONFIG["COLORS"]["BUTTON_HOVER"]
        pygame.draw.rect(screen, color, self.rect, 0, border_radius=CONFIG["STYLE"]["CORNER_RADIUS"])
        
        if not self.is_focused or (self.is_focused and blink_on):
            text_surf = self.font.render(self.text, True, CONFIG["COLORS"]["TEXT"])
            text_rect = text_surf.get_rect(center=self.rect.center)
            screen.blit(text_surf, text_rect)



# Global state
g_state_lock = threading.Lock()
g_client_state = "CONNECTING" # CONNECTING, LOGIN, LOBBY, IN_ROOM, GAME, ERROR
g_running = True
g_username = None
g_error_message = None # For login errors

# Sockets
g_lobby_socket = None
g_game_socket = None
g_game_send_queue = queue.Queue() # Queue for game inputs

# Lobby/Room State
g_lobby_data = {"users": [], "rooms": []}
g_room_data = {"id": None, "host": None, "players": []}
g_invite_popup = None # Stores invite data if one is received

# Game State
g_last_game_state = None
g_game_over_results = None
g_my_role = None # "P1" or "P2"
g_user_acknowledged_game_over = False


# Network Functions
def send_input_to_server_queue(action: str):
    """Puts a game action into the game send queue."""
    g_game_send_queue.put({"type": "INPUT", "action": action})

def game_network_thread(sock: socket.socket):
    """
    This thread handles BOTH sending and receiving for the game.
    """
    global g_last_game_state, g_game_over_results, g_running, g_client_state
    global g_game_socket, g_my_role
    logging.info("Game network thread started.")
    
    try:
        while g_running:
            # Wait for readability or a timeout to check the send queue
            readable, _, exceptional = select.select([sock], [], [sock], 0.1)

            if exceptional:
                logging.error("Game socket exception.")
                break

            # 1. Check for messages to RECEIVE
            if sock in readable:
                data_bytes = protocol.recv_msg(sock)
                if data_bytes is None:
                    logging.warning("Game server disconnected.")
                    break
                
                snapshot = json.loads(data_bytes.decode('utf-8'))
                msg_type = snapshot.get("type")
                
                if msg_type == "SNAPSHOT":
                    with g_state_lock:
                        g_last_game_state = snapshot
                
                # rrrrr Thread stops when game is over!!!!!!! >:0
                elif msg_type == "GAME_OVER":
                    logging.info(f"Game over! Results: {snapshot}")
                    with g_state_lock:
                        g_game_over_results = snapshot
                        if g_my_role == "P1": # Only the host notifies the lobby
                            send_to_lobby_queue({
                                "action": "game_over",
                                "data": {"room_id": snapshot.get("room_id")}
                            })
                    break # Game is over, stop this thread

            # 2. Check for messages to SEND
            try:
                while not g_game_send_queue.empty():
                    request = g_game_send_queue.get_nowait()
                    logging.info(f"rq: {request}")
                    json_bytes = json.dumps(request).encode('utf-8')
                    protocol.send_msg(sock, json_bytes) # Send the message

            except queue.Empty:
                pass # No more messages to send

    except (socket.error, json.JSONDecodeError, UnicodeDecodeError) as e:
        if g_running:
            logging.error(f"Error in game network thread: {e}")
    finally:
        logging.info("Game network thread waiting for user confirmation...")
        
        # Wait until the user clicks the "Back to Lobby" button
        global g_user_acknowledged_game_over
        while g_running and not g_user_acknowledged_game_over:
            time.sleep(0.1)

        logging.info("Game network thread exiting.")
        with g_state_lock:
            # Reset all game state
            if g_game_socket:
                g_game_socket.close()
            g_game_socket = None
            g_last_game_state = None
            g_game_over_results = None
            g_my_role = None
            g_user_acknowledged_game_over = False # Reset for the next game

            # The button click already set the state, but we can ensure it
            g_client_state = "LOBBY"
            
            # Refresh lobby lists now that we're back
            send_to_lobby_queue({"action": "list_rooms"})
            send_to_lobby_queue({"action": "list_users"})
            logging.info("State switched back to 'LOBBY'.")

def lobby_network_thread(host: str, port: int):
    """
    This thread handles the initial connection, plus
    BOTH sending (from a queue) and receiving (from the socket)
    for the lobby.
    """
    global g_running, g_lobby_data, g_room_data, g_invite_popup
    global g_client_state, g_game_socket, g_my_role, g_lobby_socket
    global g_error_message
    logging.info("Lobby network thread started.")
    
    sock = None
    try:
        try:
            logging.info(f"Connecting to lobby server at {host}:{port}...")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            g_lobby_socket = sock # Store the socket globally
            logging.info("Connected!")
            with g_state_lock:
                g_client_state = "LOGIN" # Connection success, move to login
        except socket.error as e:
            logging.critical(f"Failed to connect to lobby: {e}")
            with g_state_lock:
                g_error_message = f"Failed to connect: {e}"
                g_client_state = "ERROR"
            g_running = False
            return # Exit the thread
        
        # Initialize the refresh timer *inside* the try block
        last_refresh_time = time.time()
        
        # 2. Main send/receive loop for the lobby
        while g_running:
            # If the client is in the GAME state, this thread should pause
            # and let the game_network_thread handle things.
            with g_state_lock:
                if g_client_state == "GAME":
                    #time.sleep(0.5) # Sleep to prevent busy-waiting
                    continue # Skip to the next loop iteration

            # Use select to wait for readability OR a short timeout
            readable, _, exceptional = select.select([sock], [], [sock], 0.1)

            if exceptional:
                logging.error("Lobby socket exception.")
                g_running = False
                break

            # 2a. Check for messages to RECEIVE
            if sock in readable:
                data_bytes = protocol.recv_msg(sock)
                if data_bytes is None:
                    if g_running: 
                        logging.warning("Lobby server disconnected.")
                        g_running = False
                    break
                
                msg = json.loads(data_bytes.decode('utf-8'))
                #logging.info(f"(lobby): {msg}") # Log the received message
                msg_type = msg.get("type")
            
                if msg_type == "ROOM_UPDATE":
                    with g_state_lock:
                        g_room_data = msg
                        g_client_state = "IN_ROOM"

                elif msg_type == "KICKED_FROM_ROOM":
                    logging.info("KICKED MESSAGE RECEIVED!")
                    with g_state_lock:
                        g_client_state = "LOBBY"
                        g_room_data = {"id": None, "host": None, "players": []} # Reset room data
                    # Refresh lists now that we're back in the lobby
                    send_to_lobby_queue({"action": "list_rooms"})
                    send_to_lobby_queue({"action": "list_users"})
                        
                elif msg_type == "INVITE_RECEIVED":
                    with g_state_lock:
                        g_invite_popup = msg # {"from_user": ..., "room_id": ...}
                        
                elif msg_type == "GAME_START":
                    # This is the HAND-OFF!
                    game_host = msg.get("host")
                    game_port = msg.get("port")
                    logging.info(f"Hand-off received. Connecting to game at {game_host}:{game_port}")
                    
                    try:
                        # 1. Connect to new game server
                        game_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        game_sock.connect((game_host, game_port))
                        
                        # 2. Receive WELCOME
                        welcome_bytes = protocol.recv_msg(game_sock)
                        if not welcome_bytes:
                            raise Exception("Game server disconnected")
                        
                        welcome_msg = json.loads(welcome_bytes.decode('utf-8'))
                        if welcome_msg.get("type") == "WELCOME":
                            with g_state_lock:
                                g_my_role = welcome_msg.get("role")
                                g_game_socket = game_sock
                                
                            # 3. Start the game network thread
                            threading.Thread(
                                target=game_network_thread,
                                args=(g_game_socket,),
                                daemon=True
                            ).start()
                            
                            # 4. Change state
                            with g_state_lock:
                                g_client_state = "GAME"
                            
                            # 5. The game thread is now in control. This thread will pause.
                            logging.info(f"Hand-off complete. My role: {g_my_role}.")
                            # DO NOT break. The loop will now pause until the game is over.
                        
                        else:
                            raise Exception("Did not receive WELCOME from game server")
                            
                    except Exception as e:
                        logging.error(f"Game hand-off failed: {e}")
                        with g_state_lock:
                            g_client_state = "LOBBY" # Go back to lobby
                            g_error_message = "Failed to connect to game."

                elif msg_type == "gamelog_response":
                    logging.info(f"Received gamelog_response: {msg}")
                    logs = msg.get("logs", [])
                    update_records(logs, g_username)

                elif msg.get("status") == "ok":
                    reason = msg.get('reason')
                    if reason: # Only log if there *is* a reason
                        logging.info(f"Lobby OK: {reason}")
                    
                    if reason == "login_successful":
                        with g_state_lock:
                            g_client_state = "LOBBY"
                            g_error_message = None
                        # Now that we're in, refresh the lists
                        send_to_lobby_queue({"action": "list_rooms"})
                        send_to_lobby_queue({"action": "list_users"})
                    
                    # Handle list responses
                    if "rooms" in msg:
                        with g_state_lock:
                            g_lobby_data["rooms"] = msg["rooms"]
                    if "users" in msg:
                        with g_state_lock:
                            g_lobby_data["users"] = msg["users"]
                
                elif "rooms" in msg:
                    with g_state_lock:
                        g_lobby_data["rooms"] = msg["rooms"]
                
                elif "users" in msg:
                    with g_state_lock:
                        g_lobby_data["users"] = msg["users"]
                
                elif msg.get("status") == "error":
                    logging.warning(f"Lobby Error: {msg.get('reason')}")
                    with g_state_lock:
                        g_error_message = msg.get('reason')

            # 2b. Check for messages to SEND
            try:
                while not g_lobby_send_queue.empty():
                    request = g_lobby_send_queue.get_nowait()
                    json_bytes = json.dumps(request).encode('utf-8')
                    protocol.send_msg(sock, json_bytes) # Send the message
                    # logging.info(f"rq: {request}")
            except queue.Empty:
                pass # No more messages to send
            
            # 3. Check for periodic refresh
            current_time = time.time()
            if (current_time - last_refresh_time > 2):
                with g_state_lock:
                    current_state = g_client_state
                
                if current_state == "LOBBY":
                    send_to_lobby_queue({"action": "list_rooms"})
                    send_to_lobby_queue({"action": "list_users"})
                elif current_state == "IN_ROOM":
                    send_to_lobby_queue({"action": "list_users"})
                    
                last_refresh_time = current_time
            
    except (socket.error, json.JSONDecodeError, UnicodeDecodeError) as e:
        if g_running: 
            logging.error(f"Error in lobby network thread: {e}")
            g_running = False
    except Exception as e:
        if g_running: 
            logging.error(f"Unexpected lobby network thread error: {e}", exc_info=True)
            g_running = False
           
     # g_running = False # If the loop breaks, stop the app
    if g_client_state != "GAME":
        logging.info("Lobby network thread exiting.")
    # If state is GAME, the game thread is now in control

# Drawing Functions
def draw_text(surface, text, x, y, font, color):
    """Draws text using a pre-rendered font object."""
    try:
        text_surface = font.render(text, True, color)
        surface.blit(text_surface, (x, y))
    except Exception as e:
        print(f"Error rendering text: {e}")
        pass # Ignore font errors

def draw_board(surface, board_data, x_start, y_start, block_size):
    num_rows = len(board_data); num_cols = len(board_data[0])
    colors = CONFIG["COLORS"]["PIECE_COLORS"]; grid_color = CONFIG["COLORS"]["GRID_LINES"]
    
    for r in range(num_rows):
        for c in range(num_cols):
            color_id = board_data[r][c]
            rect = pygame.Rect(x_start + c * block_size, y_start + r * block_size, block_size, block_size)
            
            if color_id != 0:
                block_color = colors[color_id] if 0 <= color_id < len(colors) else (255, 255, 255)
                block_surface = get_gradient_block((block_size, block_size), block_color)
                surface.blit(block_surface, rect.topleft)
            else:
                # For empty cells, draw the faint grid line
                pygame.draw.rect(surface, grid_color, rect, 1)

def draw_game_state(surface, fonts, state, ui_elements):
    draw_text(surface, "Esc to exit", CONFIG["SCREEN"]["WIDTH"] - 120, 20, fonts["TINY"], CONFIG["COLORS"]["TEXT"])

    if state is None:
        draw_text(surface, "Connecting... Waiting for state...", 100, 100, fonts["LARGE"], CONFIG["COLORS"]["TEXT"])
        return

    pos = CONFIG["POSITIONS"]; colors = CONFIG["COLORS"]; sizes = CONFIG["SIZES"]
    
    global g_my_role, g_game_over_results
    my_key, opp_key = ("p1_state", "p2_state") if g_my_role == "P1" else ("p2_state", "p1_state")
    
    my_state = state.get(my_key, {}); opponent_state = state.get(opp_key, {})
    
    my_board = my_state.get("board")
    if my_board:
        draw_board(surface, my_board, pos["MY_BOARD"][0], pos["MY_BOARD"][1], sizes["BLOCK_SIZE"])
    
    my_piece = my_state.get("current_piece")
    if my_piece:
        shape_id = my_piece.get("shape_id", 0) + 1
        block_color = colors["PIECE_COLORS"][shape_id]
        block_surface = get_gradient_block((sizes["BLOCK_SIZE"], sizes["BLOCK_SIZE"]), block_color)
        for y, x in my_piece.get("blocks", []):
            if y >= 0:
                surface.blit(block_surface, (pos["MY_BOARD"][0] + x * sizes["BLOCK_SIZE"], pos["MY_BOARD"][1] + y * sizes["BLOCK_SIZE"]))

    opp_board = opponent_state.get("board")
    if opp_board:
        draw_board(surface, opp_board, pos["OPPONENT_BOARD"][0], pos["OPPONENT_BOARD"][1], sizes["SMALL_BLOCK_SIZE"])
        
    opp_piece = opponent_state.get("current_piece")
    if opp_piece:
        shape_id = opp_piece.get("shape_id", 0) + 1
        block_color = colors["PIECE_COLORS"][shape_id]
        block_surface = get_gradient_block((sizes["SMALL_BLOCK_SIZE"], sizes["SMALL_BLOCK_SIZE"]), block_color)
        for y, x in opp_piece.get("blocks", []):
            if y >= 0:
                surface.blit(block_surface, (pos["OPPONENT_BOARD"][0] + x * sizes["SMALL_BLOCK_SIZE"], pos["OPPONENT_BOARD"][1] + y * sizes["SMALL_BLOCK_SIZE"]))

    # Score and Lines Display
    font_obj = fonts["MEDIUM"]
    
    # My Score (Label left, Value right)
    my_right_edge = pos["MY_SCORE"][0] + 240
    draw_text(surface, "SCORE", pos["MY_SCORE"][0], pos["MY_SCORE"][1], font_obj, colors["TEXT"])
    score_surf = font_obj.render(str(my_state.get("score", 0)), True, colors["TEXT"])
    score_rect = score_surf.get_rect(topright=(my_right_edge, pos["MY_SCORE"][1]))
    surface.blit(score_surf, score_rect)

    # My Lines (Label left, Value right)
    draw_text(surface, "LINES", pos["MY_LINES"][0], pos["MY_LINES"][1], font_obj, colors["TEXT"])
    lines_surf = font_obj.render(str(my_state.get("lines", 0)), True, colors["TEXT"])
    lines_rect = lines_surf.get_rect(topright=(my_right_edge, pos["MY_LINES"][1]))
    surface.blit(lines_surf, lines_rect)

    # Opponent Score (Label left, Value right-aligned below)
    opp_right_edge = pos["OPPONENT_BOARD"][0] + (sizes["SMALL_BLOCK_SIZE"] * 10)
    draw_text(surface, "OPPONENT", pos["OPPONENT_SCORE"][0], pos["OPPONENT_SCORE"][1], font_obj, colors["TEXT"])
    opp_score_surf = font_obj.render(str(opponent_state.get("score", 0)), True, colors["TEXT"])
    opp_score_rect = opp_score_surf.get_rect(topright=(opp_right_edge, pos["OPPONENT_SCORE"][1] + 25))
    surface.blit(opp_score_surf, opp_score_rect)

    # Display remaining time
    remaining_time = state.get("remaining_time")
    if remaining_time is not None:
        draw_text(surface, "TIME", pos["TIME"][0], pos["TIME"][1], font_obj, colors["TEXT"])
        time_surf = font_obj.render(str(remaining_time), True, colors["TEXT"])
        time_rect = time_surf.get_rect(topright=(my_right_edge, pos["TIME"][1]))
        surface.blit(time_surf, time_rect)

    next_piece = my_state.get("next_piece")
    if next_piece:
        # Define new position and size for the next piece display
        next_piece_pos_x = pos["NEXT_PIECE"][0] + 20 # Move it right
        next_piece_block_size = int(sizes["BLOCK_SIZE"] * 0.85) # 15% smaller

        draw_text(surface, "NEXT", next_piece_pos_x, pos["NEXT_PIECE"][1], font_obj, colors["TEXT"])
        
        shape_id = next_piece.get("shape_id", 0) + 1
        block_color = colors["PIECE_COLORS"][shape_id]
        block_surface = get_gradient_block((next_piece_block_size, next_piece_block_size), block_color)

        for r, c in next_piece.get("blocks", []):
            # Use the new size and position for drawing
            topleft = (next_piece_pos_x + (c-2) * next_piece_block_size, 
                       pos["NEXT_PIECE"][1] + (r+2) * next_piece_block_size)
            surface.blit(block_surface, topleft)

    final_results = None
    with g_state_lock:
        if g_game_over_results: final_results = g_game_over_results

    if final_results:
        draw_game_over_screen(surface, fonts, ui_elements, final_results, my_state, opponent_state)

    elif my_state.get("game_over", False):
        draw_text(surface, "GAME OVER", pos["GAME_OVER_TEXT"][0], pos["GAME_OVER_TEXT"][1], fonts["GAME_OVER"], colors["GAME_OVER"])

def draw_game_over_screen(surface, fonts, ui_elements, final_results, my_state, opponent_state):
    """Draws a semi-transparent overlay and the game over results."""
    
    # 1. Draw semi-transparent overlay
    overlay = pygame.Surface((CONFIG["SCREEN"]["WIDTH"], CONFIG["SCREEN"]["HEIGHT"]), pygame.SRCALPHA)
    overlay.fill((20, 20, 30, 200)) # Dark, semi-transparent background
    surface.blit(overlay, (0, 0))

    # 2. Get config shortcuts
    pos = CONFIG["POSITIONS"]; colors = CONFIG["COLORS"]

    # 3. Extract results and determine reason
    winner_role = final_results.get('winner', 'Unknown')
    winner_display_name = final_results.get('winner_username', winner_role)
    reason_code = final_results.get('reason', '')
    loser_username = final_results.get('loser_username')

    reason_text = reason_code # Default to the code
    if reason_code == "board_full" and loser_username:
        reason_text = f"{loser_username}'s board is full!"
    elif reason_code == "forfeit" and loser_username:
        reason_text = f"{loser_username} abandoned the game"
    elif reason_code == "time_up":
        reason_text = "Time's up!"
    elif reason_code == "tie":
        reason_text = "Tie game!"

    if winner_display_name == "TIE":
        winner_text = "IT'S A TIE!"
    else:
        winner_text = f"WINNER: {winner_display_name}"

    p1_score = final_results.get("p1_results", {}).get("score", 0)
    p2_score = final_results.get("p2_results", {}).get("score", 0)
    score_text = f"Final Score: {p1_score} vs {p2_score}"

    # 4. Draw the text and button
    draw_text(surface, "GAME OVER", pos["GAME_OVER_TEXT"][0], pos["GAME_OVER_TEXT"][1] - 60, fonts["GAME_OVER"], colors["GAME_OVER"])
    draw_text(surface, winner_text, pos["GAME_OVER_TEXT"][0], pos["GAME_OVER_TEXT"][1], fonts["LARGE"], colors["TEXT"])
    draw_text(surface, score_text, pos["GAME_OVER_TEXT"][0], pos["GAME_OVER_TEXT"][1] + 40, fonts["MEDIUM"], colors["TEXT"])
    draw_text(surface, reason_text, pos["GAME_OVER_TEXT"][0], pos["GAME_OVER_TEXT"][1] + 80, fonts["MEDIUM"], colors["TEXT"])
    ui_elements["back_to_lobby_btn"].draw(surface)

def draw_login_screen(screen, fonts, ui_elements, blink_on):
    draw_text(screen, "Welcome to Tetris", 250, 100, fonts["LARGE"], CONFIG["COLORS"]["TEXT"])
    
    form_center_x = CONFIG["SCREEN"]["WIDTH"] // 2
    
    draw_text(screen, "Username:", form_center_x - 150, 200, fonts["SMALL"], CONFIG["COLORS"]["TEXT"])
    ui_elements["user_input"].draw(screen)
    draw_text(screen, "Password:", form_center_x - 150, 260, fonts["SMALL"], CONFIG["COLORS"]["TEXT"])
    ui_elements["pass_input"].draw(screen)
    
    ui_elements["login_btn"].draw(screen, blink_on)
    ui_elements["reg_btn"].draw(screen, blink_on)
    
    with g_state_lock:
        error_msg = g_error_message
    if error_msg:
        error_x = form_center_x - fonts["MEDIUM"].size(error_msg)[0] // 2
        draw_text(screen, error_msg, error_x, 400, fonts["MEDIUM"], CONFIG["COLORS"]["ERROR"])

    # Add signature
    signature_text = "franbrizuelab 2025"
    signature_font = fonts["TINY"]
    signature_color = (100, 100, 100) # A subtle grey
    signature_x = CONFIG["SCREEN"]["WIDTH"] // 2 - signature_font.size(signature_text)[0] // 2
    signature_y = CONFIG["SCREEN"]["HEIGHT"] - 30
    draw_text(screen, signature_text, signature_x, signature_y, signature_font, signature_color)

def draw_lobby_screen(screen, fonts, ui_elements):
    draw_text(screen, f"Lobby - Welcome {g_username}", 50, 20, fonts["LARGE"], CONFIG["COLORS"]["TEXT"])
    
    ui_elements["create_room_btn"].draw(screen)
    ui_elements["records_btn"].draw(screen)
    
    draw_text(screen, "Rooms:", 50, 150, fonts["MEDIUM"], CONFIG["COLORS"]["TEXT"])
    draw_text(screen, "Users:", 450, 150, fonts["MEDIUM"], CONFIG["COLORS"]["TEXT"])

    # Draw the users box
    users_box_rect = pygame.Rect(CONFIG["POSITIONS"]["USERS_BOX_RECT"])
    pygame.draw.rect(
        screen,
        CONFIG["COLORS"]["BACKGROUND"], # Fill with background color
        users_box_rect,
        0, # Filled rectangle
        border_radius=CONFIG["STYLE"]["CORNER_RADIUS"]
    )
    pygame.draw.rect(
        screen,
        CONFIG["STYLE"]["LOBBY_BOX_BORDER_COLOR"], # White border
        users_box_rect,
        CONFIG["STYLE"]["LOBBY_BOX_BORDER_WIDTH"], # Thin border
        border_radius=CONFIG["STYLE"]["CORNER_RADIUS"]
    )

    with g_state_lock:
        rooms = g_lobby_data.get("rooms", [])
        users = g_lobby_data.get("users", [])
    
    ui_elements["rooms_list"] = []
    for i, room in enumerate(rooms):
        y = 200 + i * 40
        room_text = f"{room['name']}'s ({room['players']}/2)"
        btn = Button(50, y, 350, 35, fonts["TINY"], room_text)
        btn.room_id = room['id']
        btn.draw(screen)
        ui_elements["rooms_list"].append(btn)
        
    ui_elements["users_list"] = []
    for i, user in enumerate(users):
        y = 200 + i * 40
        user_text = f"{user['username']} ({user['status']})"
        is_inviteable = (user['username'] != g_username and user['status'] == 'online')
        
        btn = Button(450, y, 350, 35, fonts["SMALL"], user_text)
        btn.username = user['username']
        btn.is_invite = is_inviteable
        btn.draw(screen)
        
        if is_inviteable:
            ui_elements["users_list"].append(btn)

def update_records(logs, username):
    """Processes the raw logs and updates the records_screen state."""
    processed_records = []
    for log in logs:
        user_result = None
        opponent_result = None
        for result in log["results"]:
            if result["userId"] == username:
                user_result = result
            else:
                opponent_result = result
        
        if user_result:
            winner_username = log["winner"]
            if winner_username == "P1":
                winner_username = log["users"][0]
            elif winner_username == "P2":
                winner_username = log["users"][1]

            processed_records.append({
                "date": log["start_time"].split("T")[0],
                "score": user_result["score"],
                "lines": user_result["lines"],
                "winner": winner_username,
                "opponent": opponent_result["userId"] if opponent_result else "N/A",
            })
    
    records_screen.records_state["records"] = processed_records

def draw_room_screen(screen, fonts, ui_elements):
    #draw_text(screen, "Esc to exit", CONFIG["SCREEN"]["WIDTH"] - 120, 20, None, 18, CONFIG["COLORS"]["TEXT"])
    draw_text(screen, "Esc to exit", CONFIG["SCREEN"]["WIDTH"] - 120, 20, fonts["TINY"], CONFIG["COLORS"]["TEXT"])
    
    with g_state_lock:
        room_name = g_room_data.get("name", "Room")
        players = g_room_data.get("players", [])
        host = g_room_data.get("host")
        
    draw_text(screen, f"Room: {room_name}", 50, 20, fonts["LARGE"], CONFIG["COLORS"]["TEXT"])
    
    draw_text(screen, "Players:", 50, 100, fonts["MEDIUM"], CONFIG["COLORS"]["TEXT"])
    for i, player in enumerate(players):
        text = f"P{i+1}: {player}"
        if player == host:
            text += " (Host)"
        draw_text(screen, text, 50, 150 + i * 40, fonts["MEDIUM"], CONFIG["COLORS"]["TEXT"])
    
    if len(players) < 2:
        # Only show invite list if there's space in the room
        draw_text(screen, "Invite Users:", 450, 100, fonts["MEDIUM"], CONFIG["COLORS"]["TEXT"])

        with g_state_lock:
            all_users = g_lobby_data.get("users", [])
        
        ui_elements["room_invite_list"] = []
        
        # Filter out self and players already in the room
        inviteable_users = [u for u in all_users if u['username'] not in players]

        for i, user in enumerate(inviteable_users):
            y = 150 + i * 40
            user_text = f"{user['username']} ({user['status']})"
            is_inviteable = (user['status'] == 'online')
            
            btn = Button(450, y, 350, 35, fonts["SMALL"], user_text)
            btn.username = user['username']
            btn.is_invite = is_inviteable
            btn.draw(screen)
            
            if is_inviteable:
                ui_elements["room_invite_list"].append(btn)

    if g_username == host and len(players) == 2:
        ui_elements["start_game_btn"].draw(screen)
    elif g_username == host:
        draw_text(screen, "Waiting for P2 to join...", 50, 400, fonts["MEDIUM"], CONFIG["COLORS"]["TEXT"])
    else:
        draw_text(screen, "Waiting for host to start...", 50, 400, fonts["MEDIUM"], CONFIG["COLORS"]["TEXT"])

def draw_invite_popup(screen, fonts, ui_elements):
    global g_invite_popup
    
    popup_data = None
    with g_state_lock:
        if g_invite_popup:
            popup_data = g_invite_popup.copy()

    if popup_data:
        # Draw semi-transparent overlay
        overlay = pygame.Surface((CONFIG["SCREEN"]["WIDTH"], CONFIG["SCREEN"]["HEIGHT"]), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 180))
        screen.blit(overlay, (0, 0))
        
        # Draw popup box
        popup_rect = pygame.Rect(200, 250, 600, 160)
        pygame.draw.rect(screen, CONFIG["COLORS"]["BACKGROUND"], popup_rect)
        pygame.draw.rect(screen, CONFIG["COLORS"]["TEXT"], popup_rect, 2)
        
        # Draw text
        inv_text = f"{popup_data['from_user']} invited you to a game!"
        draw_text(screen, inv_text, 220, 290, fonts["MEDIUM"], CONFIG["COLORS"]["TEXT"])
        
        # Draw buttons
        ui_elements["invite_accept_btn"].draw(screen)
        ui_elements["invite_decline_btn"].draw(screen)

# --- New Background Animation ---
class FallingPiece:
    def __init__(self, screen_width, screen_height):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.config = CONFIG["BACKGROUND_ANIMATION"]
        self.block_size = random.randint(self.config["MIN_SIZE"], self.config["MAX_SIZE"])
        self.color = random.choice(CONFIG["COLORS"]["PIECE_COLORS"][1:])
        self.speed = random.uniform(self.config["MIN_SPEED"], self.config["MAX_SPEED"])
        self.shape_id = random.randint(0, len(PIECE_SHAPES) - 1)
        self.rotation = random.randint(0, 3)
        self.x = random.randint(0, self.screen_width)
        self.y = random.uniform(-200, -50)

    def _get_blocks(self):
        """Get the (row, col) coordinates for the piece's current state."""
        shapes = PIECE_SHAPES[self.shape_id]
        shape = shapes[self.rotation % len(shapes)]
        return [(self.y + r * self.block_size, self.x + c * self.block_size) for r, c in shape]

    def update(self):
        self.y += self.speed
        if self.y > self.screen_height + 100:
            self.reset()

    def reset(self):
        self.y = random.uniform(-200, -50)
        self.x = random.randint(0, self.screen_width)
        self.block_size = random.randint(self.config["MIN_SIZE"], self.config["MAX_SIZE"])
        self.speed = random.uniform(self.config["MIN_SPEED"], self.config["MAX_SPEED"])
        self.color = random.choice(CONFIG["COLORS"]["PIECE_COLORS"][1:])
        self.shape_id = random.randint(0, len(PIECE_SHAPES) - 1)

    def draw(self, surface):
        blocks = self._get_blocks()
        for r, c in blocks:
            rect = pygame.Rect(c, r, self.block_size, self.block_size)
            
            # Create a temporary surface for each block to apply alpha
            block_surf = pygame.Surface(rect.size, pygame.SRCALPHA)
            
            # Draw the semi-transparent fill
            fill_color = self.color + (self.config["ALPHA"],)
            pygame.draw.rect(block_surf, fill_color, block_surf.get_rect())
            
            # Draw the border (slightly more opaque)
            border_color = self.color + (self.config["ALPHA"] + 50,) # Make border slightly more visible
            pygame.draw.rect(block_surf, border_color, block_surf.get_rect(), 1)
            
            surface.blit(block_surf, rect.topleft)

g_background_pieces = []

def draw_background(surface):
    surface.fill(CONFIG["COLORS"]["BACKGROUND"])
    
    global g_background_pieces
    if not g_background_pieces:
        # Initialize on first run
        num_pieces = CONFIG["BACKGROUND_ANIMATION"]["NUM_PIECES"]
        for _ in range(num_pieces):
            g_background_pieces.append(FallingPiece(CONFIG["SCREEN"]["WIDTH"], CONFIG["SCREEN"]["HEIGHT"]))

    for piece in g_background_pieces:
        piece.update()
        piece.draw(surface)

import argparse

def main():
    parser = argparse.ArgumentParser(description="Tetris GUI Client")
    parser.add_argument("--user", type=str, help="Username for automatic login")
    parser.add_argument("--password", type=str, help="Password for automatic login")
    parser.add_argument("--x", type=int, default=100, help="X position of the window")
    parser.add_argument("--y", type=int, default=100, help="Y position of the window")
    args = parser.parse_args()

    global g_running, g_client_state, g_lobby_socket, g_invite_popup
    global g_username, g_error_message, g_game_over_results
    
    # 1. Initialize Pygame
    os.environ['SDL_VIDEO_WINDOW_POS'] = f"{args.x},{args.y}"
    pygame.init()
    pygame.font.init()

    # 2. Set up screen and fonts
    screen_size = (CONFIG["SCREEN"]["WIDTH"], CONFIG["SCREEN"]["HEIGHT"])
    screen = pygame.display.set_mode(size=screen_size)
    pygame.display.set_caption("Networked Tetris")
    clock = pygame.time.Clock()

    # Load fonts
    font_path = CONFIG["FONTS"]["DEFAULT_FONT"]
    sizes = CONFIG["FONTS"]["SIZES"]
    fonts = CONFIG["FONTS"]["OBJECTS"]
    try:
        fonts["TINY"] = pygame.font.Font(font_path, sizes["TINY"])
        fonts["SMALL"] = pygame.font.Font(font_path, sizes["SMALL"])
        fonts["MEDIUM"] = pygame.font.Font(font_path, sizes["MEDIUM"])
        fonts["LARGE"] = pygame.font.Font(font_path, sizes["LARGE"])
        fonts["TITLE"] = pygame.font.Font(font_path, sizes["TITLE"])
        fonts["GAME_OVER"] = pygame.font.Font(font_path, sizes["GAME_OVER"])
        fonts["DEFAULT"] = fonts["SMALL"] # Default to small
    except pygame.error as e:
        print(f"Error loading font: {e}")
        # Fallback to default pygame font
        fonts["TINY"] = pygame.font.Font(None, sizes["TINY"])
        fonts["SMALL"] = pygame.font.Font(None, sizes["SMALL"])
        fonts["MEDIUM"] = pygame.font.Font(None, sizes["MEDIUM"])
        fonts["LARGE"] = pygame.font.Font(None, sizes["LARGE"])
        fonts["TITLE"] = pygame.font.Font(None, sizes["TITLE"])
        fonts["GAME_OVER"] = pygame.font.Font(None, sizes["GAME_OVER"])
        fonts["DEFAULT"] = fonts["SMALL"]

    # 3. Create UI elements
    form_center_x = CONFIG["SCREEN"]["WIDTH"] // 2
    input_width = 300
    ui_elements = {
        "user_input": TextInput(form_center_x - input_width // 2, 220, input_width, 32, fonts["SMALL"]),
        "pass_input": TextInput(form_center_x - input_width // 2, 280, input_width, 32, fonts["SMALL"]),
        "login_btn": Button(form_center_x - 150, 340, 140, 40, fonts["SMALL"], "Login"),
        "reg_btn": Button(form_center_x + 10, 340, 140, 40, fonts["SMALL"], "Register"),
        "create_room_btn": Button(50, 70, 200, 50, fonts["SMALL"], "Create Room"),
        "records_btn": Button(260, 70, 200, 50, fonts["SMALL"], "Records"),
        "start_game_btn": Button(50, 400, 200, 50, fonts["SMALL"], "START GAME"),
        "rooms_list": [],
        "users_list": [],
        "invite_accept_btn": Button(300, 350, 140, 40, fonts["SMALL"], "Accept"),
        "invite_decline_btn": Button(460, 350, 140, 40, fonts["SMALL"], "Decline"),
        "back_to_lobby_btn": Button(350, 450, 200, 50, fonts["SMALL"], "Back to Lobby"),
        "login_focusable_elements": ["user_input", "pass_input", "login_btn", "reg_btn"]
    }

    
    # 4. Start the lobby network thread.
    host = CONFIG["NETWORK"]["HOST"]
    port = CONFIG["NETWORK"]["PORT"]
    threading.Thread(
        target=lobby_network_thread,
        args=(host, port), # Pass host and port
    ).start()

    # Auto-login if credentials are provided
    if args.user and args.password:
        # Wait a moment for the connection to be established
        time.sleep(1) 
        send_to_lobby_queue({
            "action": "login", 
            "data": {"user": args.user, "pass": args.password}
        })
        g_username = args.user

    # 6. Main Game Loop (State Machine)
    focused_element_idx = 0 # For login screen navigation
    last_blink_time = 0
    blink_on = True
    
    while g_running:
        
        # Handle Input Events
        events = pygame.event.get()
        
        # We read the state *once* per frame for consistency
        with g_state_lock:
            popup_active = (g_invite_popup is not None)
            current_client_state = g_client_state
        
        # Handle blinking for focused buttons
        if current_client_state == "LOGIN":
            current_time = time.time()
            if current_time - last_blink_time > 0.5:
                blink_on = not blink_on
                last_blink_time = current_time
        
        if popup_active:
            # POPUP IS ACTIVE
            # Only process popup events
            for event in events:
                if event.type == pygame.QUIT:
                    g_running = False
                    
                if ui_elements["invite_accept_btn"].handle_event(event):
                    with g_state_lock:
                        room_id = g_invite_popup['room_id']
                        g_invite_popup = None # Close popup
                        g_client_state = "IN_ROOM" # Go to room
                    send_to_lobby_queue({"action": "join_room", "data": {"room_id": room_id}})

                elif ui_elements["invite_decline_btn"].handle_event(event):
                    with g_state_lock:
                        g_invite_popup = None # Close popup
            
        else:
            # NORMAL EVENT PROCESSING
            for event in events:
                if event.type == pygame.QUIT:
                    g_running = False

                # Pass events to the correct handler based on state
                if current_client_state == "LOGIN":
                    if event.type == pygame.KEYDOWN:
                        if event.key == pygame.K_TAB:
                            focused_element_idx = (focused_element_idx + 1) % len(ui_elements["login_focusable_elements"])
                            # Deactivate all text inputs, then activate the focused one if it's a text input
                            ui_elements["user_input"].active = False
                            ui_elements["pass_input"].active = False
                            ui_elements["login_btn"].is_focused = False
                            ui_elements["reg_btn"].is_focused = False

                            current_focused_name = ui_elements["login_focusable_elements"][focused_element_idx]
                            focused_element = ui_elements[current_focused_name]

                            if isinstance(focused_element, TextInput):
                                focused_element.active = True
                                focused_element.color = CONFIG["COLORS"]["INPUT_ACTIVE"]
                            elif isinstance(focused_element, Button):
                                focused_element.is_focused = True
                            
                            # Reset colors for non-focused text inputs
                            if current_focused_name != "user_input":
                                ui_elements["user_input"].color = CONFIG["COLORS"]["INPUT_BOX"]
                            if current_focused_name != "pass_input":
                                ui_elements["pass_input"].color = CONFIG["COLORS"]["INPUT_BOX"]

                        elif event.key == pygame.K_RETURN:
                            current_focused_name = ui_elements["login_focusable_elements"][focused_element_idx]
                            if current_focused_name == "login_btn":
                                user = ui_elements["user_input"].text
                                g_username = user # Store username
                                send_to_lobby_queue({"action": "login", "data": {"user": user, "pass": ui_elements["pass_input"].text}})
                                with g_state_lock:
                                    g_error_message = None # Clear old errors
                            elif current_focused_name == "reg_btn":
                                send_to_lobby_queue({"action": "register", "data": {"user": ui_elements["user_input"].text, "pass": ui_elements["pass_input"].text}})
                        else:
                            ui_elements["user_input"].handle_event(event)
                            ui_elements["pass_input"].handle_event(event)
                    else:
                        # Handle mouse clicks for login/register buttons
                        if ui_elements["login_btn"].handle_event(event):
                            user = ui_elements["user_input"].text
                            g_username = user # Store username
                            send_to_lobby_queue({"action": "login", "data": {"user": user, "pass": ui_elements["pass_input"].text}})
                            with g_state_lock:
                                g_error_message = None # Clear old errors
                            
                        if ui_elements["reg_btn"].handle_event(event):
                            send_to_lobby_queue({"action": "register", "data": {"user": ui_elements["user_input"].text, "pass": ui_elements["pass_input"].text}})
                        
                        # Also handle mouse clicks on text inputs
                        ui_elements["user_input"].handle_event(event)
                        ui_elements["pass_input"].handle_event(event)

                elif current_client_state == "LOBBY":
                    if ui_elements["create_room_btn"].handle_event(event):
                        send_to_lobby_queue({"action": "create_room", "data": {"name": f"{g_username}'s Room"}})
                        with g_state_lock:
                            g_client_state = "IN_ROOM" # Optimistesic state change
                    
                    if ui_elements["records_btn"].handle_event(event):
                        with g_state_lock:
                            g_client_state = "RECORDS"
                        records_screen.on_enter(g_username)
                        # logging.info("Called 'on_enter'")



                    for room_btn in ui_elements["rooms_list"]:
                        if room_btn.handle_event(event):
                            send_to_lobby_queue({"action": "join_room", "data": {"room_id": room_btn.room_id}})
                            # with g_state_lock:
                            #     g_client_state = "IN_ROOM" # Optimistic state change
                    
                    for user_btn in ui_elements["users_list"]:
                        if user_btn.handle_event(event) and user_btn.is_invite:
                            logging.info(f"Inviting user: {user_btn.username}")
                            send_to_lobby_queue({
                                "action": "invite",
                                "data": {"target_user": user_btn.username}
                            })
                
                elif current_client_state == "RECORDS":
                    g_client_state = records_screen.handle_records_events(event, g_state_lock, g_client_state, g_username)
                    # logging.info("Current state: 'RECORDS'")


                elif current_client_state == "IN_ROOM":
                    if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                        send_to_lobby_queue({"action": "leave_room"})
                        with g_state_lock:
                            g_client_state = "LOBBY"

                    if ui_elements["start_game_btn"].handle_event(event):
                        send_to_lobby_queue({"action": "start_game"})
                        # State will be changed to "GAME" by the network thread
                    
                    # Handle clicks on the new invite buttons in the room
                    for user_btn in ui_elements.get("room_invite_list", []):
                        if user_btn.handle_event(event) and user_btn.is_invite:
                            logging.info(f"Inviting user from room: {user_btn.username}")
                            send_to_lobby_queue({
                                "action": "invite",
                                "data": {"target_user": user_btn.username}
                            })
                
                elif current_client_state == "GAME":
                    with g_state_lock:
                        game_is_over = (g_game_over_results is not None)
                        
                    if event.type == pygame.KEYDOWN and not game_is_over:
                        if event.key == pygame.K_LEFT: send_input_to_server_queue("MOVE_LEFT")
                        elif event.key == pygame.K_RIGHT: send_input_to_server_queue("MOVE_RIGHT")
                        elif event.key == pygame.K_DOWN: send_input_to_server_queue("SOFT_DROP")
                        elif event.key == pygame.K_UP: send_input_to_server_queue("ROTATE")
                        elif event.key == pygame.K_SPACE: send_input_to_server_queue("HARD_DROP")
                        elif event.key == pygame.K_ESCAPE:
                            g_game_send_queue.put({"type": "FORFEIT"})
                    
                    if game_is_over:
                        if ui_elements["back_to_lobby_btn"].handle_event(event):
                            global g_user_acknowledged_game_over
                            with g_state_lock:
                                g_client_state = "LOBBY"
                                g_user_acknowledged_game_over = True
                                # The game network thread will now clean up g_game_over_results
        
        # Render Graphics
        fonts = CONFIG["FONTS"]["OBJECTS"]
        draw_background(screen) # Draw the new animated background

        if current_client_state == "CONNECTING":
            draw_text(screen, "Connecting to lobby...", 250, 300, fonts["TITLE"], CONFIG["COLORS"]["TEXT"])
        elif current_client_state == "LOGIN":
            draw_login_screen(screen, fonts, ui_elements, blink_on)
        elif current_client_state == "LOBBY":
            draw_lobby_screen(screen, fonts, ui_elements)
        elif current_client_state == "RECORDS":
            records_screen.draw_records_screen(screen, fonts)
        elif current_client_state == "IN_ROOM":
            draw_room_screen(screen, fonts, ui_elements)
        elif current_client_state == "GAME":
            with g_state_lock:
                state_copy = g_last_game_state.copy() if g_last_game_state else None
            # We don't call draw_background here because draw_game_state does its own fill
            draw_game_state(screen, fonts, state_copy, ui_elements)
        elif current_client_state == "ERROR":
            draw_text(screen, "Connection Error", 250, 100, fonts["GAME_OVER"], CONFIG["COLORS"]["ERROR"])
            with g_state_lock:
                error_msg = g_error_message
            if error_msg:
                draw_text(screen, error_msg, 100, 200, fonts["LARGE"], CONFIG["COLORS"]["ERROR"])
        
        draw_invite_popup(screen, fonts, ui_elements)

        # Update Display
        pygame.display.flip()
        clock.tick(CONFIG["TIMING"]["FPS"])

    # 6. Cleanup
    logging.info("Shutting down...")
    if g_lobby_socket: g_lobby_socket.close()
    if g_game_socket: g_game_socket.close()
    pygame.quit()

if __name__ == "__main__":
    main()