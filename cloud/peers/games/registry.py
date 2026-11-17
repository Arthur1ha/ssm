"""Explicit game catalogue. Add a module's GAME here to expose its metadata."""
from . import base
from .base import Game, activity_allows, game_invitation_ready
from .color_hunt import GAME as COLOR_HUNT
from .push_delivery import GAME as PUSH_DELIVERY
from .rps_siege import GAME as RPS_SIEGE

GAMES: dict[str, Game] = {game.game_id: game for game in (COLOR_HUNT, PUSH_DELIVERY, RPS_SIEGE)}

def get_game(game_id: str) -> Game | None:
    return GAMES.get(game_id) if isinstance(game_id, str) else None


def game_catalog(activity_id: str | None, sender: str, target: str) -> list[dict]:
    """Keep registered rules visible even when this pair cannot start now.

    Readiness is a current execution constraint, not a capability declaration
    or permission. Sending and accepting still recheck the live gate.
    """
    reason = base.game_invitation_block_reason(activity_id, sender, target)
    entries = []
    for game in GAMES.values():
        if not game.allows_pair(sender, target):
            continue
        try:
            available = game.available(sender, target)
        except Exception:
            available = False
        blocked = (reason if game.requires_idle else
                   None if activity_allows(activity_id, sender, target) else "activity_not_joined")
        if not available:
            blocked = "game_capability_unavailable"
        entries.append({**game.metadata(), "ready": blocked is None, "reason": blocked})
    return entries


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
