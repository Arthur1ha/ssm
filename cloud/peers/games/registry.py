"""Explicit game catalogue. Add a module's GAME here to expose its metadata."""
from .base import Game, activity_allows, game_invitation_ready
from .color_hunt import GAME as COLOR_HUNT
from .push_delivery import GAME as PUSH_DELIVERY
from .rps_siege import GAME as RPS_SIEGE

GAMES: dict[str, Game] = {game.game_id: game for game in (COLOR_HUNT, PUSH_DELIVERY, RPS_SIEGE)}

def get_game(game_id: str) -> Game | None:
    return GAMES.get(game_id) if isinstance(game_id, str) else None


def playable_games(activity_id: str | None, sender: str, target: str) -> list[dict]:
    if not activity_allows(activity_id, sender, target):
        return []
    available = []
    for game in GAMES.values():
        if not game.allows_pair(sender, target):
            continue
        try:
            ready = game.available(sender, target) and (
                not game.requires_idle or game_invitation_ready(activity_id, sender, target))
        except Exception:
            ready = False
        if ready:
            available.append(game.metadata())
    return available


def game_is_playable(activity_id: str | None, sender: str, target: str,
                     game_id: str) -> bool:
    # Capability/activity checks remain valid after a game has claimed play.
    # Requiring idle here would reject the task's own accepted execution.
    game = get_game(game_id)
    return bool(game and game.allows_pair(sender, target)
                and activity_allows(activity_id, sender, target)
                and game.available(sender, target))
