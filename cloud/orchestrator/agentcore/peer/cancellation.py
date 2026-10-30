"""Cancellation state for active peer coordination requests."""

import threading

_lock = threading.Lock()
_cancelled: set[str] = set()


def cancel_peer_coordination(request_id: str) -> None:
    with _lock:
        _cancelled.add(request_id)


def peer_coordination_cancelled(request_id: str) -> bool:
    with _lock:
        return request_id in _cancelled


def clear_peer_coordination(request_id: str) -> None:
    with _lock:
        _cancelled.discard(request_id)
