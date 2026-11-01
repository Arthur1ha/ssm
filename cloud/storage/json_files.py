"""Small durable JSON records with process and thread safe updates."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


class CorruptRecordError(ValueError):
    pass


def record_path(directory: str | Path, record_id: str) -> Path:
    if not isinstance(record_id, str) or not _SAFE_ID.fullmatch(record_id):
        raise ValueError("invalid_record_id")
    return Path(directory) / (record_id + ".json")


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked_directory(directory: str | Path):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = str(directory.resolve())
    with _LOCAL_LOCKS_GUARD:
        local = _LOCAL_LOCKS.setdefault(key, threading.RLock())
    with local:
        fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def read_record(path: Path) -> dict | None:
    def quarantine() -> None:
        # Preserve the occupied record path: reusing its ID could repeat an
        # effectful command. The copy is solely for offline diagnosis.
        duplicate = path.with_name(path.name + ".corrupt")
        if not duplicate.exists():
            shutil.copy2(path, duplicate)
            duplicate.chmod(0o600)

    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeError, OSError) as exc:
        # Keep the original path occupied so a broken ledger can never be
        # mistaken for a new task ID. A separate copy aids diagnosis.
        quarantine()
        raise CorruptRecordError(f"corrupt_json_record:{path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        quarantine()
        raise CorruptRecordError(f"unsupported_json_record:{path}")
    return data


def write_record(path: Path, data: dict, *, exclusive: bool = False) -> bool:
    """Caller holds the directory lock for read/check/write operations."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
    if exclusive:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(path.parent)
        except BaseException:
            # Leave the reserved file in place; dispatch must never proceed
            # when persistence is uncertain.
            raise
        return True
    fd, name = tempfile.mkstemp(prefix=".json-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True
