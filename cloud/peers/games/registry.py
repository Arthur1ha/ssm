"""Explicit game catalogue. Add a module's GAME here to expose its metadata."""
from .base import Game, activity_allows
from .color_hunt import GAME as COLOR_HUNT

GAMES: dict[str, Game] = {game.game_id: game for game in (COLOR_HUNT,)}

def get_game(game_id: str) -> Game | None:
    return GAMES.get(game_id) if isinstance(game_id, str) else None


def playable_games(activity_id: str | None, sender: str, target: str) -> list[dict]:
    if not activity_allows(activity_id, sender, target):
        return []
    available = []
    for game in GAMES.values():
        if (sender, target) != game.participants:
            continue
        try:
            ready = game.available(sender, target)
        except Exception:
            ready = False
        if ready:
            available.append(game.metadata())
    return available


def game_is_playable(activity_id: str | None, sender: str, target: str,
                     game_id: str) -> bool:
    return any(game["game_id"] == game_id for game in
               playable_games(activity_id, sender, target))
