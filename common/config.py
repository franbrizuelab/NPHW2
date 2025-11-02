# --- Lobby Server ---
LOBBY_HOST = '0.0.0.0'
LOBBY_PORT = 10000

# --- DB Server ---
# The Lobby will connect to this address.
DB_HOST = '127.0.0.1' 
DB_PORT = 10001

# --- Game Server ---
# The first port to try for new game servers.
# The Lobby will scan upwards from this port.
GAME_SERVER_START_PORT = 11001