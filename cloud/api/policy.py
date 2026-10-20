"""Central API action policy and minimal security audit."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

_AUDIT_FILE = Path(__file__).parent.parent / "space" / "audit.jsonl"
_audit_lock = threading.Lock()

# These names can only be reached through explicit administrator endpoints.
PERSISTENT_ACTIONS = {
    "CREATE_RULE", "UPDATE_RULE", "DELETE_RULE", "TAG_LOCATION", "SET_HOME",
    "SET_PERSONALITY", "WRITE_MEMORY", "REMEMBER_PERSON", "ADD_FACE",
    "DELETE_PERSON", "SET_AUTONOMY", "CONNECT", "DISCONNECT",
}

GO2_IMMEDIATE_ACTIONS = {
    "SPORT", "NAVIGATE", "RECOGNIZE_PERSON", "VELOCITY", "STOP",
    "StandUp", "StandDown", "Hello", "Stretch", "Dance1", "Dance2", "Move", "StopMove",
}


def ensure_instant_action(action: str) -> None:
    if action in PERSISTENT_ACTIONS:
        raise HTTPException(status_code=403, detail="persistent action requires an administrator endpoint")


def ensure_go2_action(action: str) -> None:
    ensure_instant_action(action)
    if action not in GO2_IMMEDIATE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unsupported Go2 action: {action}")


def audit_event(event: str, session: dict[str, Any] | None = None, **fields: Any) -> None:
    """Append one redacted JSON record. Tokens, cookies and request bodies are never accepted."""
    record: dict[str, Any] = {"ts": int(time.time()), "event": event}
    if session:
        record.update({"session_id": session.get("session_id"), "actor": session.get("actor")})
    for key, value in fields.items():
        if key.lower() not in {"token", "token_hash", "cookie", "access_code", "secret"}:
            record[key] = value
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _audit_lock:
        fd = os.open(_AUDIT_FILE, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, encoded.encode("utf-8"))
        finally:
            os.close(fd)
