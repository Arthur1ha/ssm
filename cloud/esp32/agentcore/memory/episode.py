"""Bounded lamp experiences; guest sessions remain in process memory only."""

import json
import threading
import time
from collections import defaultdict, deque

from cloud.esp32.paths import EPISODES_FILE
from cloud.space.execution import ExecutionContext

_lock = threading.RLock()
_ephemeral: dict[str, deque] = defaultdict(lambda: deque(maxlen=100))


def add(kind: str, detail: str, result: str, context: ExecutionContext) -> dict:
    entry = {"ts": time.time(), "kind": str(kind)[:40],
             "detail": str(detail)[:500], "result": str(result)[:40]}
    with _lock:
        if context.persist and context.actor == "admin" and context.origin_kind == "user_delegated":
            EPISODES_FILE.parent.mkdir(parents=True, exist_ok=True)
            with EPISODES_FILE.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                stream.flush()
        else:
            _ephemeral[context.session_id].append(entry)
    return entry


def recent(context: ExecutionContext, limit: int = 20) -> list[dict]:
    limit = max(1, min(limit, 50))
    with _lock:
        if context.actor == "admin" and context.persist:
            try:
                lines = EPISODES_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
                return [json.loads(line) for line in lines if line.strip()]
            except (OSError, ValueError):
                return []
        return list(_ephemeral.get(context.session_id, ()))[-limit:]


def clear_session(session_id: str) -> None:
    with _lock:
        _ephemeral.pop(session_id, None)
