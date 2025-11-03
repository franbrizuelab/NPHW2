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

# Add project root to path
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from common import config
    from common import protocol
except ImportError:
    print("Error: Could not import common/protocol.py or common/config.py.")
    sys.exit(1)

# Logging
logging.basicConfig(level=logging.INFO, format='[CLIENT_GUI] %(asctime)s - %(message)s')

# Customization Configuration
CONFIG = {
    "TIMING": {"FPS": 30},
    "SCREEN": {"WIDTH": 900, "HEIGHT": 700},
    "SIZES": {"BLOCK_SIZE": 30, "SMALL_BLOCK_SIZE": 15},
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
            (0, 0, 0), (0, 255, 255), (255, 255, 0), (128, 0, 128),
            (0, 0, 255), (255, 165, 0), (0, 255, 0), (255, 0, 0)
        ]
    },
    "POSITIONS": {
        "MY_BOARD": (50, 50), "OPPONENT_BOARD": (550, 100),
        "NEXT_PIECE": (370, 100), "MY_SCORE": (370, 50),
        "OPPONENT_SCORE": (550, 50), "MY_LINES": (370, 75),
        "OPPONENT_LINES": (550, 75), "GAME_OVER_TEXT": (100, 300)
    },
    "FONTS": {
        "DEFAULT_FONT": None, "TITLE_SIZE": 30,
        "SCORE_SIZE": 24, "GAME_OVER_SIZE": 50
    },
    "NETWORK": {
        "HOST": config.LOBBY_HOST,
        "PORT": config.LOBBY_PORT
    }
}

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
        pygame.draw.rect(screen, self.color, self.rect, 0)
        screen.blit(self.text_surface, (self.rect.x + 5, self.rect.y + 5))

class Button:
    def __init__(self, x, y, w, h, font, text=''):
        self.rect = pygame.Rect(x, y, w, h)
        self.color = CONFIG["COLORS"]["BUTTON"]
        self.text = text
        self.font = font
    
    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            if self.rect.collidepoint(event.pos):
                return True
        return False
        
    def draw(self, screen):
        color = self.color
        if self.rect.collidepoint(pygame.mouse.get_pos()):
            color = CONFIG["COLORS"]["BUTTON_HOVER"]
        pygame.draw.rect(screen, color, self.rect, 0)
        
        text_surf = self.font.render(self.text, True, CONFIG["COLORS"]["TEXT"])
        text_rect = text_surf.get_rect(center=self.rect.center)
        screen.blit(text_surf, text_rect)

# Global state
g_state_lock = threading.Lock()
g_client_state = "LOGIN" # LOGIN, LOBBY, IN_ROOM, GAME
g_running = True
g_username = None
g_error_message = None # For login errors

# Sockets
g_lobby_socket = None
g_game_socket = None
g_lobby_send_queue = queue.Queue()

# Lobby/Room State
g_lobby_data = {"users": [], "rooms": []}
g_room_data = {"id": None, "host": None, "players": []}
g_invite_popup = None # Stores invite data if one is received

# Game State
g_last_game_state = None
g_game_over_results = None
g_my_role = None # "P1" or "P2"

# Nwtwork Functions

def send_to_lobby_queue(request: dict):
    """Puts a request into the lobby send queue."""
    g_lobby_send_queue.put(request)

def send_input_to_server(action: str):
    """Sends a player action to the game server."""
    global g_game_socket, g_running
    if g_game_socket:
        try:
            message = {"type": "INPUT", "action": action}
            json_bytes = json.dumps(message).encode('utf-8')
            protocol.send_msg(g_game_socket, json_bytes)
        except socket.error as e:
            logging.warning(f"Failed to send input '{action}': {e}")
            g_running = False

def game_network_thread(sock: socket.socket):
    """
    This is the old 'network_thread_func'.
    It only listens for GAME messages.
    """
    global g_last_game_state, g_game_over_results, g_running, g_client_state
    global g_game_socket, g_my_role
    logging.info("Game network thread started.")
    
    try:
        while g_running:
            data_bytes = protocol.recv_msg(sock)
            if data_bytes is None:
                logging.warning("Game server disconnected.")
                break
                
            snapshot = json.loads(data_bytes.decode('utf-8'))
            msg_type = snapshot.get("type")
            
            if msg_type == "SNAPSHOT":
                with g_state_lock:
                    g_last_game_state = snapshot
            
            elif msg_type == "GAME_OVER":
                logging.info(f"Game over! Results: {snapshot}")
                with g_state_lock:
                    g_game_over_results = snapshot
                # We'll let the user look at the score,
                # they can press Esc to return to lobby (or we can add a button)
                # For now, we just stop receiving.
                break

    except (socket.error, json.JSONDecodeError, UnicodeDecodeError) as e:
        if g_running:
            logging.error(f"Error in game network thread: {e}")
    finally:
        logging.info("Game network thread exiting.")
        with g_state_lock:
            # Reset all game state and return to lobby
            g_client_state = "LOBBY"
            if g_game_socket:
                g_game_socket.close()
            g_game_socket = None
            g_last_game_state = None
            g_game_over_results = None
            g_my_role = None

#  This thread listens for messages from the LOBBY
def lobby_network_thread(sock: socket.socket):
    """
    This thread handles BOTH sending (from a queue)
    and receiving (from the socket) for the lobby.
    """
    global g_running, g_lobby_data, g_room_data, g_invite_popup
    global g_client_state, g_game_socket, g_my_role
    logging.info("Lobby network thread started.")
    
    while g_running:
        try:
            # Use select to wait for readability OR a short timeout
            # lets us check the send queue often
            readable, _, exceptional = select.select([sock], [], [sock], 0.1)

            if exceptional:
                logging.error("Lobby socket exception.")
                break

            # 1. Check for messages to RECEIVE
            if sock in readable:
                data_bytes = protocol.recv_msg(sock)
                if data_bytes is None:
                    if g_running: logging.warning("Lobby server disconnected.")
                    break
                
                # --- Process the received message (this logic is the same as before) ---
                msg = json.loads(data_bytes.decode('utf-8'))
                msg_type = msg.get("type")
            
                if msg_type == "ROOM_UPDATE":
                    with g_state_lock:
                        g_room_data = msg
                        
                elif msg_type == "INVITE_RECEIVED":
                    with g_state_lock:
                        g_invite_popup = msg # {"from_user": ..., "room_id": ...}
                        
                elif msg_type == "GAME_START":
                    # This is the HAND-OFF!
                    host = msg.get("host")
                    port = msg.get("port")
                    logging.info(f"Hand-off received. Connecting to game at {host}:{port}")
                    
                    try:
                        # 1. Connect to new game server
                        game_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        game_sock.connect((host, port))
                        
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
                            
                            # 5. This lobby thread is done.
                            logging.info(f"Hand-off complete. My role: {g_my_role}.")
                            break
                        
                        else:
                            raise Exception("Did not receive WELCOME from game server")
                            
                    except Exception as e:
                        logging.error(f"Game hand-off failed: {e}")
                        with g_state_lock:
                            g_client_state = "LOBBY" # Go back to lobby
                            g_error_message = "Failed to connect to game."

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
                
                elif msg.get("status") == "error":
                    logging.warning(f"Lobby Error: {msg.get('reason')}")
                    with g_state_lock:
                        g_error_message = msg.get('reason')

            # 2. Check for messages to SEND
            try:
                while not g_lobby_send_queue.empty():
                    request = g_lobby_send_queue.get_nowait()
                    json_bytes = json.dumps(request).encode('utf-8')
                    protocol.send_msg(sock, json_bytes) # Send the message
            except queue.Empty:
                pass # No more messages to send
            
        except (socket.error, json.JSONDecodeError, UnicodeDecodeError) as e:
            if g_running: logging.error(f"Error in lobby network thread: {e}")
            break
        except Exception as e:
            if g_running: logging.error(f"Unexpected lobby network thread error: {e}", exc_info=True)
            break
            
    g_running = False
    logging.info("Lobby network thread exiting.")

### ALL DRAWING FUNCTIONS ###

def draw_text(surface, text, x, y, font, size, color):
    try:
        font_obj = pygame.font.Font(font, size)
        text_surface = font_obj.render(text, True, color)
        surface.blit(text_surface, (x, y))
    except Exception:
        pass # Ignore font errors

# (draw_board is unchanged)
def draw_board(surface, board_data, x_start, y_start, block_size):
    num_rows = len(board_data); num_cols = len(board_data[0])
    colors = CONFIG["COLORS"]["PIECE_COLORS"]; grid_color = CONFIG["COLORS"]["GRID_LINES"]
    for r in range(num_rows):
        for c in range(num_cols):
            color_id = board_data[r][c]
            block_color = colors[color_id] if 0 <= color_id < len(colors) else (255, 255, 255)
            rect = (x_start + c * block_size, y_start + r * block_size, block_size, block_size)
            if color_id != 0:
                pygame.draw.rect(surface, block_color, rect, 0)
            pygame.draw.rect(surface, grid_color, rect, 1)

# (draw_game_state is unchanged, but we'll paste it for completeness)
def draw_game_state(surface, font_name, state):
    surface.fill(CONFIG["COLORS"]["BACKGROUND"])
    if state is None:
        draw_text(surface, "Connecting... Waiting for state...", 100, 100, font_name, CONFIG["FONTS"]["TITLE_SIZE"], CONFIG["COLORS"]["TEXT"])
        return

    pos = CONFIG["POSITIONS"]; colors = CONFIG["COLORS"]; sizes = CONFIG["SIZES"]; fonts = CONFIG["FONTS"]
    
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
        for y, x in my_piece.get("blocks", []):
            if y >= 0:
                rect = (pos["MY_BOARD"][0] + x * sizes["BLOCK_SIZE"], pos["MY_BOARD"][1] + y * sizes["BLOCK_SIZE"], sizes["BLOCK_SIZE"], sizes["BLOCK_SIZE"])
                pygame.draw.rect(surface, block_color, rect, 0)
                pygame.draw.rect(surface, colors["GRID_LINES"], rect, 1)

    opp_board = opponent_state.get("board")
    if opp_board:
        draw_board(surface, opp_board, pos["OPPONENT_BOARD"][0], pos["OPPONENT_BOARD"][1], sizes["SMALL_BLOCK_SIZE"])
        
    opp_piece = opponent_state.get("current_piece")
    if opp_piece:
        shape_id = opp_piece.get("shape_id", 0) + 1
        block_color = colors["PIECE_COLORS"][shape_id]
        for y, x in opp_piece.get("blocks", []):
            if y >= 0:
                rect = (pos["OPPONENT_BOARD"][0] + x * sizes["SMALL_BLOCK_SIZE"], pos["OPPONENT_BOARD"][1] + y * sizes["SMALL_BLOCK_SIZE"], sizes["SMALL_BLOCK_SIZE"], sizes["SMALL_BLOCK_SIZE"])
                pygame.draw.rect(surface, block_color, rect, 0)

    draw_text(surface, "SCORE", pos["MY_SCORE"][0], pos["MY_SCORE"][1], font_name, fonts["SCORE_SIZE"], colors["TEXT"])
    draw_text(surface, str(my_state.get("score", 0)), pos["MY_SCORE"][0], pos["MY_SCORE"][1] + 25, font_name, fonts["SCORE_SIZE"], colors["TEXT"])
    draw_text(surface, "LINES", pos["MY_LINES"][0], pos["MY_LINES"][1], font_name, fonts["SCORE_SIZE"], colors["TEXT"])
    draw_text(surface, str(my_state.get("lines", 0)), pos["MY_LINES"][0], pos["MY_LINES"][1] + 25, font_name, fonts["SCORE_SIZE"], colors["TEXT"])
    draw_text(surface, "OPPONENT", pos["OPPONENT_SCORE"][0], pos["OPPONENT_SCORE"][1], font_name, fonts["SCORE_SIZE"], colors["TEXT"])
    draw_text(surface, str(opponent_state.get("score", 0)), pos["OPPONENT_SCORE"][0], pos["OPPONENT_SCORE"][1] + 25, font_name, fonts["SCORE_SIZE"], colors["TEXT"])

    next_piece = my_state.get("next_piece")
    if next_piece:
        draw_text(surface, "NEXT", pos["NEXT_PIECE"][0], pos["NEXT_PIECE"][1], font_name, fonts["SCORE_SIZE"], colors["TEXT"])
        shape_id = next_piece.get("shape_id", 0) + 1
        block_color = colors["PIECE_COLORS"][shape_id]
        for r, c in next_piece.get("blocks", []):
            rect = (pos["NEXT_PIECE"][0] + (c-2) * sizes["BLOCK_SIZE"], pos["NEXT_PIECE"][1] + (r+2) * sizes["BLOCK_SIZE"], sizes["BLOCK_SIZE"], sizes["BLOCK_SIZE"])
            pygame.draw.rect(surface, block_color, rect, 0)
            pygame.draw.rect(surface, colors["GRID_LINES"], rect, 1)

    final_results = None
    with g_state_lock:
        if g_game_over_results: final_results = g_game_over_results
    if final_results:
        winner_text = f"WINNER: {final_results.get('winner', 'Unknown')}"
        draw_text(surface, winner_text, pos["GAME_OVER_TEXT"][0], pos["GAME_OVER_TEXT"][1], font_name, fonts["GAME_OVER_SIZE"], colors["GAME_OVER"])
    elif my_state.get("game_over", False):
        draw_text(surface, "GAME OVER", pos["GAME_OVER_TEXT"][0], pos["GAME_OVER_TEXT"][1], font_name, fonts["GAME_OVER_SIZE"], colors["GAME_OVER"])

# Draw functions (Lobby)
def draw_login_screen(screen, font_small, font_large, ui_elements):
    screen.fill(CONFIG["COLORS"]["BACKGROUND"])
    draw_text(screen, "Welcome to Tetris", 250, 100, None, CONFIG["FONTS"]["TITLE_SIZE"], CONFIG["COLORS"]["TEXT"])
    
    draw_text(screen, "Username:", 250, 200, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["TEXT"])
    ui_elements["user_input"].draw(screen)
    draw_text(screen, "Password:", 250, 260, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["TEXT"])
    ui_elements["pass_input"].draw(screen)
    
    ui_elements["login_btn"].draw(screen)
    ui_elements["reg_btn"].draw(screen)
    
    if g_error_message:
        draw_text(screen, g_error_message, 250, 400, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["ERROR"])

def draw_lobby_screen(screen, font_small, font_large, ui_elements):
    screen.fill(CONFIG["COLORS"]["BACKGROUND"])
    draw_text(screen, f"Lobby - Welcome {g_username}", 50, 20, None, CONFIG["FONTS"]["TITLE_SIZE"], CONFIG["COLORS"]["TEXT"])
    
    ui_elements["create_room_btn"].draw(screen)
    
    draw_text(screen, "Rooms:", 50, 150, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["TEXT"])
    draw_text(screen, "Users:", 450, 150, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["TEXT"])

    # Draw room list
    with g_state_lock:
        ui_elements["rooms_list"] = []
        for i, room in enumerate(g_lobby_data.get("rooms", [])):
            y = 200 + i * 40
            room_text = f"{room['name']} ({room['players']}/2) - Host: {room['host']}"
            btn = Button(50, y, 350, 35, font_small, room_text)
            btn.room_id = room['id'] # Attach data to the button
            btn.draw(screen)
            ui_elements["rooms_list"].append(btn)
            
        # Draw user list
        with g_state_lock:
            ui_elements["users_list"] = [] # Clear old buttons
            for i, user in enumerate(g_lobby_data.get("users", [])):
                y = 200 + i * 40
                user_text = f"{user['username']} ({user['status']})"
                
                # Make the button clickable only if it's not you and is online
                is_inviteable = (user['username'] != g_username and user['status'] == 'online')
                
                btn = Button(450, y, 350, 35, font_small, user_text)
                btn.username = user['username']
                btn.is_invite = is_inviteable
                btn.draw(screen)
                
                if is_inviteable:
                    ui_elements["users_list"].append(btn)

def draw_room_screen(screen, font_small, font_large, ui_elements):
    screen.fill(CONFIG["COLORS"]["BACKGROUND"])
    
    with g_state_lock:
        room_name = g_room_data.get("name", "Room")
        players = g_room_data.get("players", [])
        host = g_room_data.get("host")
        
    draw_text(screen, f"Room: {room_name}", 50, 20, None, CONFIG["FONTS"]["TITLE_SIZE"], CONFIG["COLORS"]["TEXT"])
    
    draw_text(screen, "Players:", 50, 100, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["TEXT"])
    for i, player in enumerate(players):
        text = f"P{i+1}: {player}"
        if player == host:
            text += " (Host)"
        draw_text(screen, text, 50, 150 + i * 40, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["TEXT"])
    
    if len(players) < 2:
        draw_text(screen, "Waiting for P2...", 50, 230, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["TEXT"])
    
    if g_username == host and len(players) == 2:
        ui_elements["start_game_btn"].draw(screen)
    elif g_username == host:
        draw_text(screen, "Waiting for P2 to start...", 50, 400, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["TEXT"])
    else:
        draw_text(screen, "Waiting for host to start...", 50, 400, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["TEXT"])

# Draws the invite popup if one is active
def draw_invite_popup(screen, font_small, ui_elements):
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
        popup_rect = pygame.Rect(250, 250, 400, 160)
        pygame.draw.rect(screen, CONFIG["COLORS"]["BACKGROUND"], popup_rect)
        pygame.draw.rect(screen, CONFIG["COLORS"]["TEXT"], popup_rect, 2)
        
        # Draw text
        inv_text = f"Invite from {popup_data['from_user']}"
        draw_text(screen, inv_text, 270, 270, font_small, CONFIG["FONTS"]["TITLE_SIZE"], CONFIG["COLORS"]["TEXT"])
        draw_text(screen, f"to join room {popup_data['room_id']}?", 270, 310, font_small, CONFIG["FONTS"]["SCORE_SIZE"], CONFIG["COLORS"]["TEXT"])
        
        # Draw buttons
        ui_elements["invite_accept_btn"].draw(screen)
        ui_elements["invite_decline_btn"].draw(screen)

def main():
    global g_running, g_client_state, g_lobby_socket, g_invite_popup
    global g_username, g_error_message
    
    # 1. Initialize Pygame
    pygame.init()
    pygame.font.init()

    # 2. Set up screen and fonts
    screen_size = (CONFIG["SCREEN"]["WIDTH"], CONFIG["SCREEN"]["HEIGHT"])
    screen = pygame.display.set_mode(size=screen_size)
    pygame.display.set_caption("Networked Tetris")
    clock = pygame.time.Clock()
    font_small = pygame.font.Font(None, 24)
    font_large = pygame.font.Font(None, 36)

    # 3. Create UI elements
    ui_elements = {
        "user_input": TextInput(250, 220, 300, 32, font_small),
        "pass_input": TextInput(250, 280, 300, 32, font_small),
        "login_btn": Button(250, 340, 140, 40, font_small, "Login"),
        "reg_btn": Button(410, 340, 140, 40, font_small, "Register"),
        "create_room_btn": Button(50, 70, 200, 50, font_small, "Create Room"),
        "start_game_btn": Button(50, 400, 200, 50, font_small, "START GAME"),
        "rooms_list": [],
        "users_list": [],
        "invite_accept_btn": Button(300, 350, 140, 40, font_small, "Accept"),
        "invite_decline_btn": Button(460, 350, 140, 40, font_small, "Decline"),
    }
    
    # 4. Connect to Lobby Server
    host = CONFIG["NETWORK"]["HOST"]
    port = CONFIG["NETWORK"]["PORT"]
    try:
        logging.info(f"Connecting to lobby server at {host}:{port}...")
        g_lobby_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        g_lobby_socket.connect((host, port))
        logging.info("Connected!")
    except socket.error as e:
        logging.critical(f"Failed to connect to lobby: {e}")
        g_error_message = f"Failed to connect to lobby: {e}"
        g_client_state = "ERROR"
    
    # 5. Start the lobby network thread
    if g_client_state != "ERROR":
        threading.Thread(
            target=lobby_network_thread,
            args=(g_lobby_socket,),
            daemon=True
        ).start()

    # 6. Main Game Loop (State Machine)
    while g_running:
        
        # Handle Input Events
        events = pygame.event.get()
        
        popup_active = False
        with g_state_lock:
            popup_active = (g_invite_popup is not None)
        
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
            # Skip all other event processing
            
        else:
            # NORMAL EVENT PROCESSING
            for event in events:
                if event.type == pygame.QUIT:
                    g_running = False

                # Pass events to the correct handler based on state
                if g_client_state == "LOGIN":
                    ui_elements["user_input"].handle_event(event)
                    ui_elements["pass_input"].handle_event(event)
                    
                    if ui_elements["login_btn"].handle_event(event):
                        user = ui_elements["user_input"].text
                        g_username = user # Store username
                        send_to_lobby_queue({"action": "login", "data": {"user": user, "pass": ui_elements["pass_input"].text}})
                        with g_state_lock:
                            g_error_message = None # Clear old errors
                        
                    if ui_elements["reg_btn"].handle_event(event):
                        send_to_lobby_queue({"action": "register", "data": {"user": ui_elements["user_input"].text, "pass": ui_elements["pass_input"].text}})

                elif g_client_state == "LOBBY":
                    if ui_elements["create_room_btn"].handle_event(event):
                        send_to_lobby_queue({"action": "create_room", "data": {"name": f"{g_username}'s Room"}})
                        g_client_state = "IN_ROOM" # Optimistic state change
                    
                    for room_btn in ui_elements["rooms_list"]:
                        if room_btn.handle_event(event):
                            send_to_lobby_queue({"action": "join_room", "data": {"room_id": room_btn.room_id}})
                            g_client_state = "IN_ROOM" # Optimistic state change
                    
                    for user_btn in ui_elements["users_list"]:
                        if user_btn.handle_event(event) and user_btn.is_invite:
                            logging.info(f"Inviting user: {user_btn.username}")
                            send_to_lobby_queue({
                                "action": "invite",
                                "data": {"target_user": user_btn.username}
                            })
                
                elif g_client_state == "IN_ROOM":
                    if ui_elements["start_game_btn"].handle_event(event):
                        send_to_lobby_queue({"action": "start_game"})
                        # State will be changed to "GAME" by the network thread
                
                elif g_client_state == "GAME":
                    # This is the old input handler
                    if event.type == pygame.KEYDOWN and g_game_over_results is None:
                        if event.key == pygame.K_LEFT: send_input_to_server("MOVE_LEFT")
                        elif event.key == pygame.K_RIGHT: send_input_to_server("MOVE_RIGHT")
                        elif event.key == pygame.K_DOWN: send_input_to_server("SOFT_DROP")
                        elif event.key == pygame.K_UP: send_input_to_server("ROTATE")
                        elif event.key == pygame.K_SPACE: send_input_to_server("HARD_DROP")
                        elif event.key == pygame.K_ESCAPE: 
                            # User can press Esc to leave game
                            if g_game_socket:
                                g_game_socket.close() # This will trigger the game_network_thread to exit

        
        # Render Graphics
        if g_client_state == "LOGIN":
            draw_login_screen(screen, font_small, font_large, ui_elements)
        elif g_client_state == "LOBBY":
            draw_lobby_screen(screen, font_small, font_large, ui_elements)
        elif g_client_state == "IN_ROOM":
            draw_room_screen(screen, font_small, font_large, ui_elements)
        elif g_client_state == "GAME":
            with g_state_lock:
                state_copy = g_last_game_state.copy() if g_last_game_state else None
            draw_game_state(screen, CONFIG["FONTS"]["DEFAULT_FONT"], state_copy)
        elif g_client_state == "ERROR":
            screen.fill(CONFIG["COLORS"]["BACKGROUND"])
            draw_text(screen, "Connection Error", 250, 100, None, 50, CONFIG["COLORS"]["ERROR"])
            if g_error_message:
                draw_text(screen, g_error_message, 100, 200, font_small, 30, CONFIG["COLORS"]["ERROR"])
        
        draw_invite_popup(screen, font_small, ui_elements)

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