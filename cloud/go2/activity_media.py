"""Bounded, private JPEG references for Go2 activity questions."""

from __future__ import annotations

import re
import time
import threading
from collections import OrderedDict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "space" / "data" / "activity_media"
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_IMAGES = 30
_MAX_BYTES = 2_000_000
_TEMP_IMAGES: OrderedDict[tuple[str, str], tuple[float, bytes]] = OrderedDict()
_TEMP_LOCK = threading.Lock()
_TEMP_TTL_S = 900


def save_temporary_snapshot(activity_id: str, question_id: str, frame: bytes | None) -> str | None:
    """Keep search evidence briefly in memory, including visitor observations."""
    if (not _ID.fullmatch(activity_id) or not _ID.fullmatch(question_id)
            or not isinstance(frame, bytes) or not 0 < len(frame) <= _MAX_BYTES):
        return None
    with _TEMP_LOCK:
        now = time.monotonic()
        for key, (expires, _) in tuple(_TEMP_IMAGES.items()):
            if expires <= now:
                _TEMP_IMAGES.pop(key, None)
        key = (activity_id, question_id)
        _TEMP_IMAGES[key] = (now + _TEMP_TTL_S, frame)
        _TEMP_IMAGES.move_to_end(key)
        while len(_TEMP_IMAGES) > _MAX_IMAGES:
            _TEMP_IMAGES.popitem(last=False)
    return f"/api/go2/activity-media/{activity_id}/{question_id}"


def save_snapshot(activity_id: str, question_id: str, frame: bytes | None) -> str | None:
    if (not _ID.fullmatch(activity_id) or not _ID.fullmatch(question_id)
            or not isinstance(frame, bytes) or not 0 < len(frame) <= _MAX_BYTES):
        return None
    _ROOT.mkdir(parents=True, exist_ok=True)
    path = _ROOT / f"{activity_id}_{question_id}.jpg"
    path.write_bytes(frame)
    images = sorted(_ROOT.glob("*.jpg"), key=lambda item: item.stat().st_mtime)
    for old in images[:-_MAX_IMAGES]:
        old.unlink(missing_ok=True)
    return f"/api/go2/activity-media/{activity_id}/{question_id}"


def load_snapshot(activity_id: str, question_id: str) -> bytes | None:
    if not _ID.fullmatch(activity_id) or not _ID.fullmatch(question_id):
        return None
    with _TEMP_LOCK:
        item = _TEMP_IMAGES.get((activity_id, question_id))
        if item is not None:
            if item[0] > time.monotonic():
                return item[1]
            _TEMP_IMAGES.pop((activity_id, question_id), None)
            return None
    path = _ROOT / f"{activity_id}_{question_id}.jpg"
    return path.read_bytes() if path.is_file() else None
