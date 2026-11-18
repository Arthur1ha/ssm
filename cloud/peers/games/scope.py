"""Trusted lifetime of a game invocation, backed by the activity journal.

The scope carries correlation, never permissions or another game state store.
Role Graphs and message adapters recheck the original invitation before effects.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class GameScope:
    activity_id: str
    invitation_id: str
    game_id: str
    terminal: bool = False


_scope: ContextVar[GameScope | None] = ContextVar("game_scope", default=None)


def current_game_scope() -> GameScope | None:
    return _scope.get()


def current_terminal_scope() -> GameScope | None:
    scope = _scope.get()
    return scope if scope is not None and scope.terminal else None


def game_scope_available() -> bool:
    scope = current_game_scope()
    if scope is None:
        return True
    if scope.terminal:
        # A closing game's final expressions never regain action/message
        # permissions. Only the role adapter's pure submission can proceed.
        return False
    from cloud.space.activity import get_activity_store
    return get_activity_store().game_allows(scope.activity_id, scope.invitation_id)


def terminal_scope_available() -> bool:
    scope = current_terminal_scope()
    if scope is None:
        return False
    from cloud.space.activity import get_activity_store, game_instance_id
    store = get_activity_store()
    activity = store.current()
    return bool(activity and activity.get("activity_id") == scope.activity_id
                and activity.get("status") in {"starting", "active", "partial"}
                and scope.invitation_id not in activity.get("ended_game_instances", [])
                and not any(row.get("event_type") == "game_ended"
                            and game_instance_id(row) == scope.invitation_id
                            for row in store.events(scope.activity_id)))


@contextmanager
def game_scope(activity_id: str, invitation_id: str, game_id: str):
    token = _scope.set(GameScope(activity_id, invitation_id, game_id))
    try:
        yield _scope.get()
    finally:
        _scope.reset(token)


@contextmanager
def terminal_game_scope(activity_id: str, invitation_id: str, game_id: str):
    token = _scope.set(GameScope(activity_id, invitation_id, game_id, terminal=True))
    try:
        yield _scope.get()
    finally:
        _scope.reset(token)


def begin_game_close() -> None:
    """Revoke new decisions/messages before waiting for physical cleanup."""
    scope = current_game_scope()
    if scope is not None:
        from cloud.space.activity import get_activity_store
        get_activity_store().record_event(
            scope.activity_id, "game_closing", game_id=scope.game_id,
            game_instance_id=scope.invitation_id, trigger_message_id=scope.invitation_id,
            context_scope="process", display=False,
            source_event_id="game:closing:" + scope.invitation_id)
