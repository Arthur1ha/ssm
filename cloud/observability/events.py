"""The single task-event contract and its privacy boundary."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator

from .logging_config import write_agent_event

EVENTS = frozenset({
    "task.received", "run.started", "persona.loaded", "persona.changed",
    "model.request", "model.response", "model.decision", "model.error",
    "tool.dispatch", "tool.result", "peer.dispatch", "peer.result",
    "device.command", "device.result", "reply.draft", "reply.validated",
    "reply.fallback", "reply.emitted", "run.paused", "run.resumed",
    "run.cancelled", "run.finished",
})
CONTEXT_FIELDS = frozenset({
    "service", "agent_id", "trace_id", "run_id", "parent_run_id",
    "session_id", "turn", "step_id", "request_id", "task_id", "origin_kind",
})
_context: ContextVar[dict[str, Any]] = ContextVar("ssm_agent_event_context", default={})


def current_event_context() -> dict[str, Any]:
    """Return a copy for authenticated cross-process propagation."""
    return dict(_context.get())
_sensitive_key = re.compile(r"password|passwd|secret|api.?key|authorization|cookie|credential|pairing|invite|access.?token|refresh.?token", re.I)
_private_content = re.compile(r"^(?:image|frame|audio|base64|private_memory|full_memory|memory_content)$", re.I)
_inline_secret = re.compile(r"(?i)(bearer\s+|(?:api[_-]?key|password|passwd|token|secret|cookie|authorization)\s*[:=]\s*)[^\s,;]+")
_long_base64 = re.compile(r"(?:data:[\w/+.-]+;base64,)?[A-Za-z0-9+/]{256,}={0,2}")
MAX_FIELD_CHARS = 8192


@contextmanager
def bind_context(**fields: Any) -> Iterator[None]:
    """Bind correlation IDs for this async context; restore them on exit."""
    invalid = set(fields) - CONTEXT_FIELDS
    if invalid:
        raise ValueError(f"Unsupported event context fields: {sorted(invalid)}")
    token = _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def _secret_values() -> tuple[str, ...]:
    return tuple(v for k, v in os.environ.items() if len(v) >= 6 and _sensitive_key.search(k))


def _redact_text(value: str, secrets: tuple[str, ...]) -> Any:
    value = _inline_secret.sub(lambda m: m.group(1) + "[REDACTED]", value)
    value = _long_base64.sub(
        lambda m: "[BINARY_REDACTED]" if m.group(0).startswith("data:")
        or any(char in m.group(0) for char in "+/=") else m.group(0), value)
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    if len(value) <= MAX_FIELD_CHARS:
        return value
    return {"preview": value[:MAX_FIELD_CHARS], "length": len(value),
            "sha256": hashlib.sha256(value.encode()).hexdigest(), "truncated": True}


def _sanitize(value: Any, secrets: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 12:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            if _sensitive_key.search(name):
                result[name] = "[REDACTED]"
            elif _private_content.match(name):
                result[name] = {"type": name, "omitted": True,
                                "size": len(item) if hasattr(item, "__len__") else None}
            else:
                result[name] = _sanitize(item, secrets, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, secrets, depth + 1) for item in value[:100]] + (
            [{"truncated": True, "length": len(value)}] if len(value) > 100 else [])
    if isinstance(value, bytes):
        return {"type": "bytes", "size": len(value), "omitted": True}
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_text(str(value), secrets)


def emit_agent_event(event: str, **fields: Any) -> None:
    """Emit one allowlisted JSONL event after recursive redaction and bounds."""
    if event not in EVENTS:
        raise ValueError(f"Unknown agent event: {event}")
    combined = {**_context.get(), **fields}
    combined = {key: value for key, value in combined.items() if value is not None}
    combined.pop("event", None)
    combined.pop("ts", None)
    combined.pop("level", None)
    combined.setdefault("service", os.getenv("SSM_LOG_SERVICE", "api"))
    safe = _sanitize(combined, _secret_values())
    for key, value in tuple(safe.items()):
        if isinstance(value, (dict, list)):
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            if len(encoded) > MAX_FIELD_CHARS:
                safe[key] = {"preview": encoded[:MAX_FIELD_CHARS], "length": len(encoded),
                             "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                             "truncated": True}
    try:
        write_agent_event({"event": event,
                           "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                           "level": "info", **safe})
    except OSError as exc:
        logging.getLogger(__name__).warning("Session event log write failed: %s", type(exc).__name__)
