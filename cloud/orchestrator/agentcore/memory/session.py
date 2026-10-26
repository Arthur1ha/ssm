"""Bounded per-session conversation history."""

import threading

# ── 对话历史 ─────────────────────────────────────────────────────
_conversation_history: dict[str, list] = {}
_MAX_HISTORY = 10
_lock = threading.RLock()

def _append_history(session_id: str, user_msg: str, response: str):
    """Append to one session only; histories are never shared globally."""
    with _lock:
        history = _conversation_history.setdefault(session_id, [])
        history.append({"user": user_msg[:2000], "assistant": response[:2000]})
        while len(history) > _MAX_HISTORY:
            history.pop(0)


def recent_history(session_id: str) -> list[dict]:
    with _lock:
        return list(_conversation_history.get(session_id, []))


def clear_session_context(session_id: str) -> None:
    """Forget the graph's ephemeral conversation state for one session."""
    with _lock:
        _conversation_history.pop(session_id, None)


