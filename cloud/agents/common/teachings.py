"""Partitioned, inert owner teachings shared by cloud agents.

Only the API writes. Agent processes call list_for and never interpret a
remembered record as an executable rule.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2] / "data" / "teachings"
_SEGMENT = re.compile(r"^[^/\\\x00-\x1f]{1,80}$")
_lock = threading.RLock()
logger = logging.getLogger(__name__)
MAX_RECORDS = 500


def _path(space_id: str, unit_id: str) -> Path:
    if (not isinstance(space_id, str) or not isinstance(unit_id, str)
            or not _SEGMENT.fullmatch(space_id) or not _SEGMENT.fullmatch(unit_id)
            or space_id in {".", ".."} or unit_id in {".", ".."}):
        raise ValueError("invalid teaching partition")
    return Path(os.environ.get("SSM_TEACHINGS_DIR", str(_ROOT))) / space_id / f"{unit_id}.json"


def _text(value: str, field: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {field}")
    return value.strip()


def list_for(unit_id: str, *, space_id: str = "main") -> list[dict]:
    path = _path(space_id, unit_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Teaching partition unreadable: %s", path)
        raise
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        logger.error("Teaching partition has invalid shape: %s", path)
        raise ValueError(f"corrupt teaching partition: {path}")
    return data


def _save(space_id: str, unit_id: str, items: list[dict]) -> None:
    path = _path(space_id, unit_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".teachings-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(items, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def remember(unit_id: str, name: str, trigger: str, behavior: str, source_text: str,
             *, space_id: str = "main", source: str = "owner_confirmation",
             legacy_id: str | None = None, legacy_file: str | None = None,
             legacy_index: int | None = None, legacy_backup: str | None = None,
             status: str = "remembered", confirmation_id: str | None = None) -> dict:
    name, trigger, behavior = (_text(name, "name", 120), _text(trigger, "trigger"),
                               _text(behavior, "behavior"))
    source_text = _text(source_text, "source_text", 1000)
    with _lock:
        items = list_for(unit_id, space_id=space_id)
        if confirmation_id is not None:
            confirmation_id = _text(confirmation_id, "confirmation_id", 120)
            prior = next((item for item in items if item.get("confirmation_id") == confirmation_id), None)
            if prior is not None:
                return prior
        if len(items) >= MAX_RECORDS:
            raise ValueError("teaching limit reached")
        now = time.time()
        item = {"schema_version": 1, "teaching_id": f"tb_{uuid.uuid4().hex}",
                "unit_id": unit_id, "space_id": space_id, "name": name,
                "trigger": trigger, "behavior": behavior, "status": status,
                "execution_status": "unbound",
                "source": source, "source_text": source_text,
                "created_at": now, "updated_at": now, "revision": 1}
        if confirmation_id is not None:
            item["confirmation_id"] = confirmation_id
        if legacy_id is not None:
            item["legacy_id"] = str(legacy_id)[:120]
        if legacy_file is not None:
            item["legacy_file"] = str(legacy_file)[:300]
        if legacy_index is not None:
            item["legacy_index"] = legacy_index
        if legacy_backup is not None:
            item["legacy_backup"] = str(legacy_backup)[:300]
        items.append(item)
        _save(space_id, unit_id, items)
        return item


def revise(unit_id: str, teaching_id: str, changes: dict, *, space_id: str = "main") -> dict | None:
    if not changes or set(changes) - {"name", "trigger", "behavior"}:
        raise ValueError("invalid revision fields")
    validated = {key: _text(value, key, 120 if key == "name" else 1000 if key == "source_text" else 500)
                 for key, value in changes.items()}
    with _lock:
        items = list_for(unit_id, space_id=space_id)
        for item in items:
            if item.get("teaching_id") == teaching_id:
                item.update(validated)
                item.pop("binding", None)
                item["execution_status"] = "unbound"
                item["revision"] += 1
                item["updated_at"] = time.time()
                _save(space_id, unit_id, items)
                return item
    return None


def set_binding(unit_id: str, teaching_id: str, binding: dict | None,
                *, enabled: bool = False, space_id: str = "main") -> dict | None:
    with _lock:
        items = list_for(unit_id, space_id=space_id)
        for item in items:
            if item.get("teaching_id") == teaching_id:
                if binding is None:
                    item.pop("binding", None)
                    item["execution_status"] = "unbound"
                else:
                    item["binding"] = binding
                    item["execution_status"] = "enabled" if enabled else "ready"
                item["revision"] += 1
                item["updated_at"] = time.time()
                _save(space_id, unit_id, items)
                return item
    return None


def forget(unit_id: str, teaching_id: str, *, space_id: str = "main") -> bool:
    with _lock:
        items = list_for(unit_id, space_id=space_id)
        kept = [item for item in items if item.get("teaching_id") != teaching_id]
        if len(kept) == len(items):
            return False
        _save(space_id, unit_id, kept)
        return True
