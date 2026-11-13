"""Peer games; public imports remain available from cloud.peers.games."""
from .base import Game, activity_allows
from .registry import GAMES, game_is_playable, get_game, playable_games

__all__ = ["Game", "GAMES", "activity_allows", "game_is_playable", "get_game", "playable_games"]
