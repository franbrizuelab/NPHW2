# Implements the "Length-Prefixed Framing Protocol"
# Protocol Format:
# [ 4-byte Header ] [ N-byte Body ]
# - Header: A 4-byte unsigned integer ('!I') in network byte order
#             (big-endian), specifying the length of the body.
# - Body: N bytes of data (e.g., a UTF-8 encoded JSON string).

import socket
import struct
import logging

# Constants

# Header is 4 bytes, unsigned int, network byte order 
HEADER_FORMAT = '!I'
HEADER_LENGTH = struct.calcsize(HEADER_FORMAT)

# Max message size is 64 KiB
MAX_MSG_SIZE = 65536

# Configure logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

# Private Helper Function

def _recv_all(sock: socket.socket, length: int) -> bytes | None:
    """
    Private helper to  receive 'length' bytes from a socket.
    This handles partial 'recv()' calls
    """
    chunks = []
    bytes_received = 0
    while bytes_received < length:
        # Calculate remaining bytes needed
        bytes_to_read = length - bytes_received
        
        # In blocking mode, recv will wait for data
        chunk = sock.recv(bytes_to_read)
        
        if not chunk:
            # Socket was closed prematurely
            logging.error(f"Socket closed unexpectedly while waiting for {length} bytes. "
                          f"Received {bytes_received} bytes so far.")
            return None
        
        chunks.append(chunk)
        bytes_received += len(chunk)
        
    # Join all chunks to form the complete message
    return b''.join(chunks)

# Public API Functions

def send_msg(sock: socket.socket, message_bytes: bytes):
    """
    Sends a message using the length-prefixed protocol.
    
    1. Checks message size.
    2. Packs the length into a 4-byte header.
    3. Sends the header.
    4. Sends the message body.
    """
    # Get the length of the message body
    length = len(message_bytes)

    # 1. Check message size against the limit 
    if length > MAX_MSG_SIZE:
        raise ValueError(f"Message size ({length} bytes) exceeds limit ({MAX_MSG_SIZE} bytes)")

    # 2. Pack the length into a 4-byte header 
    header_bytes = struct.pack(HEADER_FORMAT, length)

    try:
        # 3. Send the header
        sock.sendall(header_bytes)
        
        # 4. Send the message body
        # sendall() handles partial sends for us 
        sock.sendall(message_bytes)
        
        # logging.info(f"Sent: {length} bytes (Payload: {message_bytes[:50]}...)")

    except socket.error as e:
        # Handle cases like "Broken pipe" if the other side disconnected
        logging.error(f"Socket error during send: {e}")
        raise
    except Exception as e:
        logging.error(f"An unexpected error occurred during send: {e}")
        raise

def recv_msg(sock: socket.socket) -> bytes | None:
    """
    Receives a message using the length-prefixed protocol.
    
    1. Reads 4 bytes to get the header.
    2. Unpacks the header to get the body length.
    3. Validates the length.
    4. Reads N bytes to get the full body.
    
    Returns the message body as bytes, or None if the peer disconnected.
    """
    try:
        # 1. Read the 4-byte header
        header_bytes = _recv_all(sock, HEADER_LENGTH)
        if header_bytes is None:
            # Peer disconnected gracefully before sending header
            # logging.info("Peer disconnected (recv header).")
            return None

        # 2. Unpack the header to get the body length
        body_length = struct.unpack(HEADER_FORMAT, header_bytes)[0]

        # 3. Validate the length 
        if not (0 < body_length <= MAX_MSG_SIZE):
            logging.error(f"Invalid message length received: {body_length}. Closing connection.")
            # This is a protocol violation. We should close the socket.
            sock.close()
            return None
        
        # logging.info(f"Header received. Expecting {body_length} byte body...")

        # 4. Read N bytes to get the full body
        body_bytes = _recv_all(sock, body_length)
        
        if body_bytes is None:
            # Peer disconnected gracefully before sending full body
            logging.warning(f"Peer disconnected after sending header for {body_length} bytes.")
            return None

        # logging.info(f"Received: {body_length} bytes (Payload: {body_bytes[:50]}...)")
        return body_bytes

    except (socket.error, struct.error) as e:
        logging.error(f"Error during recv: {e}")
        return None
    except Exception as e:
        logging.error(f"An unexpected error occurred during recv: {e}")
        return None
