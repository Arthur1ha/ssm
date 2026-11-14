"""Trusted correlation for one invocation of an existing role Graph.

This carries no permissions and owns no agent loop or durable state. Tool
adapters inherit the turn through the shared runner's copied context.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class PeerTurn:
    messenger: object
    message: dict
    sent: bool = False
    deferred_game: str | None = None


_current_turn: ContextVar[PeerTurn | None] = ContextVar("peer_turn", default=None)


def current_peer_turn(messenger, sender: str, target: str) -> PeerTurn | None:
    turn = _current_turn.get()
    if (turn is not None and turn.messenger is messenger
            and (sender, target) == (turn.message["target"], turn.message["sender"])):
        return turn
    return None


@contextmanager
def peer_turn(messenger, message: dict):
    turn = PeerTurn(messenger, dict(message))
    token = _current_turn.set(turn)
    try:
        yield turn
    finally:
        _current_turn.reset(token)
