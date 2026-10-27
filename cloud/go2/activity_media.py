"""Bounded, private JPEG references for Go2 activity questions."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "space" / "data" / "activity_media"
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_IMAGES = 30
_MAX_BYTES = 2_000_000


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
    path = _ROOT / f"{activity_id}_{question_id}.jpg"
    return path.read_bytes() if path.is_file() else None
