# Self-contained, non-networked, non-GUI
# Logic for a single game of Tetris.
# The game server will create two instances of this class
# and run them as the authoritative state.

import random

# Constants 
BOARD_WIDTH = 10
BOARD_HEIGHT = 20

# Standard Tetris piece shapes and their rotations
# 0: I, 1: O, 2: T, 3: J, 4: L, 5: S, 6: Z
# Each tuple represents (row, col) offsets from a pivot point 
PIECE_SHAPES = (
    # I
    (((0, -2), (0, -1), (0, 0), (0, 1)),  # 0 deg
     ((-2, 0), (-1, 0), (0, 0), (1, 0))), # 90 deg
    # O
    (((0, 0), (0, 1), (1, 0), (1, 1)),),  
    # T
    (((0, -1), (0, 0), (0, 1), (1, 0)),   # 0 deg
     ((-1, 0), (0, 0), (1, 0), (0, -1)),  # 90 deg
     ((0, -1), (0, 0), (0, 1), (-1, 0)),  # 180 deg
     ((-1, 0), (0, 0), (1, 0), (0, 1))),   # 270 deg
    # J
    (((0, -1), (0, 0), (0, 1), (-1, 1)),  # 0 deg
     ((-1, 0), (0, 0), (1, 0), (1, 1)),   # 90 deg
     ((0, -1), (0, 0), (0, 1), (1, -1)),  # 180 deg
     ((-1, -1), (-1, 0), (0, 0), (1, 0))), # 270 deg
    # L
    (((0, -1), (0, 0), (0, 1), (-1, -1)), 
     ((-1, 0), (0, 0), (1, 0), (1, -1)),  
     ((0, -1), (0, 0), (0, 1), (1, 1)),   
     ((-1, 1), (-1, 0), (0, 0), (1, 0))),  
    # S
    (((0, -1), (0, 0), (1, 0), (1, 1)),   
     ((-1, 1), (0, 0), (0, 1), (1, 0))),   
    # Z
    (((0, 0), (0, 1), (1, -1), (1, 0)),   
     ((-1, 0), (0, 0), (0, 1), (1, 1)))  
)

# Scoring: {lines_cleared: points}
SCORING = {
    0: 0,
    1: 100,
    2: 300,
    3: 500,
    4: 800
}

#  Helper Class 
class Piece:
    """Represents a single falling Tetris piece."""
    def __init__(self, shape_id: int):
        self.shape_id = shape_id
        self.shapes = PIECE_SHAPES[shape_id]
        self.rotation = 0
        
        # Spawn position
        self.x = BOARD_WIDTH // 2
        self.y = 0 if shape_id != 0 else 1 # 'I' piece spawns a bit higher

    def get_blocks(self):
        """Get the (row, col) coordinates for the piece's current state."""
        shape = self.shapes[self.rotation % len(self.shapes)]
        # Absolute (r,c) coordinates for each block 
        return [(self.y + r, self.x + c) for r, c in shape]

    def get_next_rotation(self):
        """Get the coordinates for the next rotation state."""
        next_rot = (self.rotation + 1) % len(self.shapes)
        shape = self.shapes[next_rot]
        return [(self.y + r, self.x + c) for r, c in shape]

#  Main Game Class 

class TetrisGame:
    """Manages the state of one Tetris board."""
    
    def __init__(self, seed: int):
        self.board = self._create_empty_board()
        self.score = 0
        self.lines_cleared = 0
        self.game_over = False
        
        # Use a seedable RNG for deterministic piece sequences
        self._rng = random.Random(seed)
        self._bag = []
        
        self.next_piece = self._get_from_bag()
        self.current_piece = None
        self._spawn_new_piece()

    def _create_empty_board(self):
        # 0 represents an empty cell
        return [[0 for _ in range(BOARD_WIDTH)] for _ in range(BOARD_HEIGHT)]

    def _get_from_bag(self):
        """Implements the 7-bag piece randomizer."""
        if not self._bag:
            # Refill the bag when empty
            self._bag = list(range(len(PIECE_SHAPES)))
            self._rng.shuffle(self._bag)
        
        # Return a new Piece object
        return Piece(self._bag.pop())

    def _spawn_new_piece(self):
        """Promotes next_piece to current and checks for game over."""
        self.current_piece = self.next_piece
        self.next_piece = self._get_from_bag()
        
        # Check for game over (spawn collision)
        if self._check_collision(self.current_piece.get_blocks()):
            self.game_over = True
            # Set piece to None so it doesn't get drawn
            self.current_piece = None

    def _check_collision(self, blocks: list[tuple[int, int]]) -> bool:
        """Checks if a piece's blocks are in an invalid position."""
        for y, x in blocks:
            # Check wall bounds
            if x < 0 or x >= BOARD_WIDTH:
                return True
            # Check floor bounds (only bottom)
            if y >= BOARD_HEIGHT:
                return True
            # Check board (only for visible rows)
            if y >= 0 and self.board[y][x] != 0:
                return True
        return False

    def _lock_piece(self):
        """Stamps the current piece onto the board."""
        if self.current_piece is None:
            return
            
        blocks = self.current_piece.get_blocks()
        
        for y, x in blocks:
            # Only lock blocks that are on the visible board
            if 0 <= y < BOARD_HEIGHT and 0 <= x < BOARD_WIDTH:
                # Use shape_id + 1 as the color/block ID
                self.board[y][x] = self.current_piece.shape_id + 1
        
        self._clear_lines()
        self._spawn_new_piece()

    def _clear_lines(self):
        """Checks for and clears completed lines."""
        new_board = []
        lines_to_clear = []
        
        # Find full lines from bottom up
        for r_idx in range(BOARD_HEIGHT - 1, -1, -1):
            row = self.board[r_idx]
            if 0 not in row:
                lines_to_clear.append(r_idx)
            else:
                new_board.insert(0, row)
        
        lines_count = len(lines_to_clear)
        if lines_count > 0:
            # Add points
            self.score += SCORING.get(lines_count, 0)
            self.lines_cleared += lines_count
            # Add new empty rows at the top
            for _ in range(lines_count):
                new_board.insert(0, [0 for _ in range(BOARD_WIDTH)])
            
            self.board = new_board

    #  Public API (called by Game Server) 

    def move(self, direction: str):
        """Move the current piece 'left' or 'right'."""
        if self.game_over or self.current_piece is None:
            return

        dx = -1 if direction == 'left' else 1
        
        # Get blocks at new position
        new_blocks = [(y, x + dx) for y, x in self.current_piece.get_blocks()]
        
        if not self._check_collision(new_blocks):
            # Commit the move
            self.current_piece.x += dx

    def rotate(self):
        """Rotate the current piece clockwise."""
        if self.game_over or self.current_piece is None:
            return
            
        new_blocks = self.current_piece.get_next_rotation()
        
        # This is a simple rotation, no complex wall kicks
        if not self._check_collision(new_blocks):
            # Commit the rotation
            self.current_piece.rotation += 1

    # ...
    def tick(self):
        self.soft_drop()

    def soft_drop(self):
        """Move the current piece down by one, or lock if it collides."""
        if self.game_over or self.current_piece is None:
            return
            
        # Get blocks at new position
        new_blocks = [(y + 1, x) for y, x in self.current_piece.get_blocks()]
        
        if self._check_collision(new_blocks):
            # Landed. Lock the piece.
            self._lock_piece()
        else:
            # Commit the move
            self.current_piece.y += 1
    
    def hard_drop(self):
        """Instantly drop and lock the piece. (Optional method)."""
        if self.game_over or self.current_piece is None:
            return
        
        # Keep moving down until we collide
        while not self._check_collision([(y + 1, x) for y, x in self.current_piece.get_blocks()]):
            self.current_piece.y += 1
            
        # Once we'd collide on the next drop, lock it
        self._lock_piece()

    def get_state_snapshot(self) -> dict:
        """
        Returns the complete state of the game as a
        JSON-serializable dictionary for the server to broadcast.
        """
        
        # Get current piece info (if it exists)
        current_piece_data = None
        if self.current_piece:
            current_piece_data = {
                "shape_id": self.current_piece.shape_id,
                "blocks": self.current_piece.get_blocks()
            }
        
        # Get next piece info
        next_piece_data = {
            "shape_id": self.next_piece.shape_id,
            # We only need to show the shape, not its position
            "blocks": [(r, c + 3) for r, c in PIECE_SHAPES[self.next_piece.shape_id][0]]
        }
            
        return {
            "board": self.board,
            "score": self.score,
            "lines": self.lines_cleared,
            "game_over": self.game_over,
            "current_piece": current_piece_data,
            "next_piece": next_piece_data
        }