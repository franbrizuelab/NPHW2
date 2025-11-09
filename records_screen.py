import pygame
from common import config
from shared import send_to_lobby_queue
from client_gui import CONFIG, Button

records_state = {
    "view": "user",  # "user" or "global"
    "records": [],
    "sort_by": "date",
    "sort_order": "desc",
    "headers": {
        "date": Button(50, 100, 200, 40, None, "Date"),
        "score": Button(260, 100, 120, 40, None, "Score"),
        "lines": Button(390, 100, 120, 40, None, "Lines"),
        "winner": Button(520, 100, 180, 40, None, "Winner"),
        "opponent": Button(710, 100, 180, 40, None, "Opponent"),
    }
}


def draw_text(surface, text, x, y, font, color):
    """Draws text using a pre-rendered font object."""
    try:
        text_surface = font.render(text, True, color)
        surface.blit(text_surface, (x, y))
    except Exception as e:
        print(f"Error rendering text: {e}")
        pass # Ignore font errors

def fetch_records(username):
    """Sends a request to the lobby server to fetch game logs."""
    send_to_lobby_queue({
        "action": "query_gamelogs",
        "data": {"userId": username}
    })


def draw_records_screen(screen, fonts):
    draw_text(screen, "Records", 50, 20, fonts["LARGE"], (255, 255, 255))
    draw_text(screen, "Esc to exit", CONFIG["SCREEN"]["WIDTH"] - 120, 20, fonts["TINY"], (255, 255, 255))

    # Draw headers
    for header_name, header_button in records_state["headers"].items():
        header_button.font = fonts["SMALL"]
        header_button.draw(screen)

    # Draw records
    y_offset = 150
    for record in records_state["records"]:
        draw_text(screen, record["date"], 60, y_offset, fonts["SMALL"], (255, 255, 255))
        draw_text(screen, str(record["score"]), 270, y_offset, fonts["SMALL"], (255, 255, 255))
        draw_text(screen, str(record["lines"]), 400, y_offset, fonts["SMALL"], (255, 255, 255))
        draw_text(screen, record["winner"], 530, y_offset, fonts["SMALL"], (255, 255, 255))
        draw_text(screen, record["opponent"], 720, y_offset, fonts["SMALL"], (255, 255, 255))
        y_offset += 40
        pygame.draw.line(screen, (100, 100, 100), (50, y_offset - 10), (CONFIG["SCREEN"]["WIDTH"] - 50, y_offset - 10), 1)


def handle_records_events(event, g_state_lock, g_client_state, username):
    if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
        with g_state_lock:
            g_client_state = "LOBBY"
    
    if event.type == pygame.MOUSEBUTTONDOWN:
        for header_name, header_button in records_state["headers"].items():
            if header_button.handle_event(event):
                if header_name == "winner":
                    # Not sortable, but we can add other functionality here later
                    return g_client_state

                if records_state["sort_by"] == header_name:
                    records_state["sort_order"] = "asc" if records_state["sort_order"] == "desc" else "desc"
                else:
                    records_state["sort_by"] = header_name
                    records_state["sort_order"] = "desc"
                
                # Sort the records
                records_state["records"].sort(
                    key=lambda x: x[records_state["sort_by"]],
                    reverse=records_state["sort_order"] == "desc"
                )


    return g_client_state

def on_enter(username):
    """Called when entering the records screen."""
    fetch_records(username)

