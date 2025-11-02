# Pygame for graphics.
# Connects to a Game Server.
# Sends user inputs (keys) to the server.
# Receives "SNAPSHOT" messages in a separate thread.
# Only renders the state it receives, runs NO game logic.

import pygame
import socket
import threading
import json
import sys
import os
import time
import logging


# import from the 'common' folder
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from common import protocol
    # import config, but override server details for now
    from common import config 
except ImportError:
    print("Error: Could not import common/protocol.py or common/config.py.")
    print("Ensure this file is in a folder next to the 'common' folder.")
    sys.exit(1)

# Logging
logging.basicConfig(level=logging.INFO, format='[CLIENT_GUI] %(asctime)s - %(message)s')

# Customization!
# All colors, positions, and sizes are defined here.
CONFIG = {
    # Screen and Timing
    "TIMING": {
        "FPS": 30  # Frames per second
    },
    "SCREEN": {
        "WIDTH": 900,
        "HEIGHT": 700
    },
    
    # Board and Block Sizes
    "SIZES": {
        "BLOCK_SIZE": 30,      # Pixel size for one block on the main board
        "SMALL_BLOCK_SIZE": 15 # Pixel size for the opponent's board
    },

    # Colors (RGB tuples)
    "COLORS": {
        "BACKGROUND": (20, 20, 30),
        "GRID_LINES": (40, 40, 50),
        "TEXT": (255, 255, 255),
        "GAME_OVER": (255, 0, 0),
        # Color 0 is empty. Colors 1-7 match the piece_id + 1
        "PIECE_COLORS": [
            (0, 0, 0),        # 0: Empty (will be hidden by background)
            (0, 255, 255),    # 1: I (Cyan)
            (255, 255, 0),    # 2: O (Yellow)
            (128, 0, 128),    # 3: T (Purple)
            (0, 0, 255),      # 4: J (Blue)
            (255, 165, 0),    # 5: L (Orange)
            (0, 255, 0),      # 6: S (Green)
            (255, 0, 0)       # 7: Z (Red)
        ]
    },

    # Positions (X, Y top-left coordinates)
    "POSITIONS": {
        "MY_BOARD": (50, 50),
        "OPPONENT_BOARD": (550, 100),
        "NEXT_PIECE": (370, 100),
        "MY_SCORE": (370, 50),
        "OPPONENT_SCORE": (550, 50),
        "MY_LINES": (370, 75),
        "OPPONENT_LINES": (550, 75),
        "GAME_OVER_TEXT": (100, 300)
    },
    
    # Fonts
    "FONTS": {
        "DEFAULT_FONT": None, # None = use Pygame's default font
        "TITLE_SIZE": 30,
        "SCORE_SIZE": 24,
        "GAME_OVER_SIZE": 50
    },
    
    # Network
    "NETWORK": {
        # This client connects directly to the game server
        # (TODO) Connect to the lobby server before handing off to the game server
        "HOST": '127.0.0.1',
        "PORT": config.GAME_SERVER_START_PORT # Use 11001 from config
    }
}

# Global State
# This state is shared between the main thread (rendering)
# and the network thread (receiving)
g_last_game_state = None
g_state_lock = threading.Lock()
g_server_socket = None
g_running = True # Global flag to stop threads

# Network Functions

def network_thread_func(sock: socket.socket):
    """
    Runs in a separate thread.
    Continuously receives messages from the server.
    Updates the global game state.
    """
    global g_last_game_state, g_running
    logging.info("Network thread started.")
    
    while g_running:
        try:
            # recv_msg is blocking, which is perfect for a thread
            data_bytes = protocol.recv_msg(sock)
            
            if data_bytes is None:
                logging.warning("Server disconnected.")
                break # Server closed connection
                
            # Decode and parse the snapshot
            data_str = data_bytes.decode('utf-8')
            snapshot = json.loads(data_str)
            
            # We only care about SNAPSHOT messages here
            if snapshot.get("type") == "SNAPSHOT":
                # Use a lock to safely update the global state
                with g_state_lock:
                    g_last_game_state = snapshot
            
            # (TODO) Handle other messages like "GAME_OVER", "CHAT", etc.)

        except (socket.error, json.JSONDecodeError, UnicodeDecodeError) as e:
            if g_running:
                logging.error(f"Error in network thread: {e}")
            break
        except Exception as e:
            if g_running:
                logging.error(f"Unexpected error in network thread: {e}", exc_info=True)
            break
            
    g_running = False # Signal main thread to stop
    logging.info("Network thread exiting.")

def send_input_to_server(action: str):
    """Sends a player action to the game server."""
    global g_server_socket
    if g_server_socket is None or not g_running:
        return
        
    try:
        # Just send the input
        message = {
            "type": "INPUT",
            "action": action
        }
        json_bytes = json.dumps(message).encode('utf-8')
        protocol.send_msg(g_server_socket, json_bytes)
        
    except socket.error as e:
        logging.warning(f"Failed to send input '{action}': {e}")
        global g_running
        g_running = False # Stop the game if we can't send data

# Drawing Functions

def draw_text(surface, text, x, y, font, size, color):
    """Helper function to draw text on the screen."""
    try:
        # Use a specific font if provided, else default
        font_obj = pygame.font.Font(font, size)
        text_surface = font_obj.render(text, True, color)
        surface.blit(text_surface, (x, y))
    except Exception as e:
        logging.warning(f"Failed to render text: {e}")
        # Fallback to default font
        default_font = pygame.font.Font(None, 24)
        text_surface = default_font.render(text, True, (255, 0, 0))
        surface.blit(text_surface, (x, y))

def draw_board(surface, board_data, x_start, y_start, block_size):
    """
    Draws a 10x20 game board.
    board_data is a 2D list (20 rows, 10 cols)
    """
    num_rows = len(board_data)
    if num_rows == 0:
        return
    num_cols = len(board_data[0])
    
    # Board dimensions
    width_px = num_cols * block_size
    height_px = num_rows * block_size
    
    colors = CONFIG["COLORS"]["PIECE_COLORS"]
    grid_color = CONFIG["COLORS"]["GRID_LINES"]
    
    # Draw each block
    for r in range(num_rows):
        for c in range(num_cols):
            color_id = board_data[r][c]
            
            # Get the block's color
            block_color = colors[color_id] if 0 <= color_id < len(colors) else (255, 255, 255)
            
            # Define the rectangle
            rect = (
                x_start + c * block_size, # x
                y_start + r * block_size, # y
                block_size,               # width
                block_size                # height
            )
            
            if color_id != 0:
                # Draw the filled block
                pygame.draw.rect(
                    surface,
                    block_color,
                    rect,
                    0 # 0 = fill
                )
            
            # Draw the grid outline for all blocks
            pygame.draw.rect(
                surface,
                grid_color,
                rect,
                1 # 1 = 1px outline
            )

def draw_game_state(surface, font_name, state):
    """The main rendering function. Draws everything."""
    
    # 1. Fill background
    surface.fill(CONFIG["COLORS"]["BACKGROUND"])
    
    # 2. Check if we have state
    if state is None:
        draw_text(
            surface,
            "Connecting... Waiting for state...",
            100, 100,
            font_name, CONFIG["FONTS"]["TITLE_SIZE"], CONFIG["COLORS"]["TEXT"]
        )
        return

    # 3. Get all config shortcuts
    pos = CONFIG["POSITIONS"]
    colors = CONFIG["COLORS"]
    sizes = CONFIG["SIZES"]
    fonts = CONFIG["FONTS"]
    
    # Get correct state based on role
    global g_my_role
    if g_my_role == "P1":
        my_key, opp_key = "p1_state", "p2_state"
    elif g_my_role == "P2":
        my_key, opp_key = "p2_state", "p1_state"
    else:
        # Default view if role is  unknown
        my_key, opp_key = "p1_state", "p2_state"
    
    my_state = state.get(my_key, {})
    opponent_state = state.get(opp_key, {})
    # END

    # 5. Draw Player 1 (My Board) 
    my_board = my_state.get("board")
    if my_board:
        draw_board(
            surface, my_board,
            pos["MY_BOARD"][0], pos["MY_BOARD"][1],
            sizes["BLOCK_SIZE"]
        )
    
    # Use my_state
    my_piece = my_state.get("current_piece")
    if my_piece:
        shape_id = my_piece.get("shape_id", 0) + 1
        block_color = colors["PIECE_COLORS"][shape_id]
        
        for y, x in my_piece.get("blocks", []):
            if y >= 0:
                rect = (
                    pos["MY_BOARD"][0] + x * sizes["BLOCK_SIZE"],
                    pos["MY_BOARD"][1] + y * sizes["BLOCK_SIZE"],
                    sizes["BLOCK_SIZE"], sizes["BLOCK_SIZE"]
                )
                pygame.draw.rect(surface, block_color, rect, 0)
                pygame.draw.rect(surface, colors["GRID_LINES"], rect, 1)

    # 6. Draw Player 2 (Opponent's Board)
    # Use opponent_state
    opp_board = opponent_state.get("board")
    if opp_board:
        draw_board(
            surface, opp_board,
            pos["OPPONENT_BOARD"][0], pos["OPPONENT_BOARD"][1],
            sizes["SMALL_BLOCK_SIZE"]
        )
        
    # Use opponent_state
    opp_piece = opponent_state.get("current_piece")
    if opp_piece:
        shape_id = opp_piece.get("shape_id", 0) + 1
        block_color = colors["PIECE_COLORS"][shape_id]
        
        for y, x in opp_piece.get("blocks", []):
            if y >= 0:
                rect = (
                    pos["OPPONENT_BOARD"][0] + x * sizes["SMALL_BLOCK_SIZE"],
                    pos["OPPONENT_BOARD"][1] + y * sizes["SMALL_BLOCK_SIZE"],
                    sizes["SMALL_BLOCK_SIZE"], sizes["SMALL_BLOCK_SIZE"]
                )
                pygame.draw.rect(surface, block_color, rect, 0)

    # 7. Draw Text Info (Scores, Lines)
    # Use my_state and opponent_state
    draw_text(surface, "SCORE", pos["MY_SCORE"][0], pos["MY_SCORE"][1], font_name, fonts["SCORE_SIZE"], colors["TEXT"])
    draw_text(surface, str(my_state.get("score", 0)), pos["MY_SCORE"][0], pos["MY_SCORE"][1] + 25, font_name, fonts["SCORE_SIZE"], colors["TEXT"])
    
    draw_text(surface, "LINES", pos["MY_LINES"][0], pos["MY_LINES"][1], font_name, fonts["SCORE_SIZE"], colors["TEXT"])
    draw_text(surface, str(my_state.get("lines", 0)), pos["MY_LINES"][0], pos["MY_LINES"][1] + 25, font_name, fonts["SCORE_SIZE"], colors["TEXT"])

    draw_text(surface, "OPPONENT", pos["OPPONENT_SCORE"][0], pos["OPPONENT_SCORE"][1], font_name, fonts["SCORE_SIZE"], colors["TEXT"])
    draw_text(surface, str(opponent_state.get("score", 0)), pos["OPPONENT_SCORE"][0], pos["OPPONENT_SCORE"][1] + 25, font_name, fonts["SCORE_SIZE"], colors["TEXT"])

    # 8. Draw 'Next' Piece
    # Use my_state
    next_piece = my_state.get("next_piece")
    if next_piece:
        shape_id = next_piece.get("shape_id", 0) + 1
        block_color = colors["PIECE_COLORS"][shape_id]
        
        for r, c in next_piece.get("blocks", []):
            rect = (
                pos["NEXT_PIECE"][0] + (c-2) * sizes["BLOCK_SIZE"],
                pos["NEXT_PIECE"][1] + (r+2) * sizes["BLOCK_SIZE"],
                sizes["BLOCK_SIZE"], sizes["BLOCK_SIZE"]
            )
            pygame.draw.rect(surface, block_color, rect, 0)
            pygame.draw.rect(surface, colors["GRID_LINES"], rect, 1)

    # 9. Draw Game Over
    # Use my_state
    if my_state.get("game_over", False):
        draw_text(
            surface, "GAME OVER",
            pos["GAME_OVER_TEXT"][0], pos["GAME_OVER_TEXT"][1],
            font_name, fonts["GAME_OVER_SIZE"], colors["GAME_OVER"]
        )

# Main Function

def main():
    global g_server_socket, g_running, g_last_game_state, g_my_role # Added g_my_role
    
    # 1. Initialize Pygame
    pygame.init()
    pygame.font.init()

    # 2. Set up the screen
    screen_size = (CONFIG["SCREEN"]["WIDTH"], CONFIG["SCREEN"]["HEIGHT"])
    screen = pygame.display.set_mode(
        size=screen_size,
        flags=0,
        depth=0
    )
    pygame.display.set_caption("Networked Tetris Client")
    clock = pygame.time.Clock()
    
    # 3. Connect to Game Server
    host = CONFIG["NETWORK"]["HOST"]
    port = CONFIG["NETWORK"]["PORT"]
    try:
        logging.info(f"Connecting to game server at {host}:{port}...")
        g_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        g_server_socket.connect((host, port))
        logging.info("Connected! Waiting for WELCOME...")
    except socket.error as e:
        logging.critical(f"Failed to connect to server: {e}")
        pygame.quit()
        return

    # Receive WELCOME message
    try:
        # We must receive the WELCOME message before starting the game
        welcome_bytes = protocol.recv_msg(g_server_socket)
        if welcome_bytes is None:
            logging.critical("Server disconnected before sending WELCOME.")
            pygame.quit()
            return
            
        welcome_msg = json.loads(welcome_bytes.decode('utf-8'))
        
        if welcome_msg.get("type") == "WELCOME":
            g_my_role = welcome_msg.get("role")
            seed = welcome_msg.get("seed")
            logging.info(f"Successfully joined game. My role: {g_my_role}. Seed: {seed}")
        else:
            logging.error(f"Expected WELCOME, got: {welcome_msg}")
            pygame.quit()
            return
            
    except Exception as e:
        logging.critical(f"Error receiving WELCOME message: {e}")
        pygame.quit()
        return

    # 4. Start the network thread
    net_thread = threading.Thread(
        target=network_thread_func,
        args=(g_server_socket,),
        daemon=True
    )
    net_thread.start()

    # 5. Main Game Loop
    while g_running:
        
        # ... (Handle Input Events - NO CHANGE NEEDED) ...
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                g_running = False
            
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_LEFT:
                    send_input_to_server("MOVE_LEFT")
                elif event.key == pygame.K_RIGHT:
                    send_input_to_server("MOVE_RIGHT")
                elif event.key == pygame.K_DOWN:
                    send_input_to_server("SOFT_DROP")
                elif event.key == pygame.K_UP:
                    send_input_to_server("ROTATE")
                elif event.key == pygame.K_SPACE:
                    send_input_to_server("HARD_DROP")
                elif event.key == pygame.K_ESCAPE:
                    g_running = False

        # Render Graphics
        current_state = None
        with g_state_lock:
            if g_last_game_state:
                current_state = g_last_game_state.copy()
        
        draw_game_state(
            surface=screen,
            font_name=CONFIG["FONTS"]["DEFAULT_FONT"],
            state=current_state
        )

        pygame.display.flip()
        clock.tick(CONFIG["TIMING"]["FPS"])

    # 6. Cleanup
    logging.info("Shutting down...")
    if g_server_socket:
        g_server_socket.close()
    pygame.quit()

if __name__ == "__main__":
    main()