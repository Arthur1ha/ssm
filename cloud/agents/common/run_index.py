"""Minimal durable mapping from a public run ID to its Graph thread.

Graph checkpoints contain the conversation and tool state. This index stores
only routing identifiers so API and MQTT processes can resume the same thread.
"""

import asyncio
from contextlib import asynccontextmanager
from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import time


class FileRunIndex:
    def __init__(self, root: str | Path | None = None) -> None:
        default = Path(__file__).resolve().parents[2] / "data" / "run_index"
        self.root = Path(root or os.getenv("SSM_RUN_INDEX_DIR", default))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _paths(self, run_id: str) -> tuple[Path, Path]:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id is required")
        name = sha256(run_id.encode()).hexdigest()
        return self.root / f"{name}.json", self.root / f"{name}.lock"

    def _admission_path(self, agent_id: str, session_id: str) -> Path:
        if any(not isinstance(value, str) or not value for value in (agent_id, session_id)):
            raise ValueError("agent_id and session_id are required")
        name = sha256(f"{agent_id}\0{session_id}".encode()).hexdigest()
        return self.root / f"{name}.admission.lock"

    @contextmanager
    def session_lock(self, agent_id: str, session_id: str):
        """Serialize admission and resume checks for one role/session."""
        fd = os.open(self._admission_path(agent_id, session_id), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @asynccontextmanager
    async def async_session_lock(self, agent_id: str, session_id: str):
        """Wait for the same cross-process lock without blocking the event loop."""
        fd = os.open(self._admission_path(agent_id, session_id), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def _locked(self, run_id: str, *, exclusive: bool):
        path, lock_path = self._paths(run_id)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _read(path: Path, run_id: str) -> dict | None:
        try:
            with path.open("r", encoding="utf-8") as source:
                row = json.load(source)
        except FileNotFoundError:
            return None
        if row.get("run_id") != run_id or not all(
            isinstance(row.get(key), str) and row[key]
            for key in ("agent_id", "session_id", "thread_id")
        ):
            raise ValueError("invalid_run_index_record")
        return row

    def _write(self, path: Path, row: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".run-", suffix=".tmp", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(row, target, ensure_ascii=False, separators=(",", ":"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            dir_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def put_if_absent(self, run_id: str, agent_id: str, session_id: str,
                      thread_id: str) -> dict:
        if any(not isinstance(value, str) or not value
               for value in (agent_id, session_id, thread_id)):
            raise ValueError("agent_id, session_id, and thread_id are required")
        with self._locked(run_id, exclusive=True) as path:
            existing = self._read(path, run_id)
            if existing is not None:
                if (existing["agent_id"], existing["session_id"], existing["thread_id"]) != (
                    agent_id, session_id, thread_id
                ):
                    raise ValueError("run_id_conflict")
                return {**existing, "created": False}
            row = {"run_id": run_id, "agent_id": agent_id, "session_id": session_id,
                   "thread_id": thread_id, "created_at": time.time()}
            self._write(path, row)
            return {**row, "created": True}

    def mark_checkpoint(self, run_id: str, session_id: str,
                        checkpoint_id: str) -> dict:
        """Pin a finished run so a later run in the same thread cannot hide it."""
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("checkpoint_id is required")
        with self._locked(run_id, exclusive=True) as path:
            row = self._read(path, run_id)
            if row is None or row["session_id"] != session_id:
                raise ValueError("run_not_found")
            previous = row.get("checkpoint_id")
            if previous is not None and previous != checkpoint_id:
                raise ValueError("checkpoint_conflict")
            if previous is None:
                row["checkpoint_id"] = checkpoint_id
                self._write(path, row)
            return row

    def get(self, run_id: str, session_id: str, *, agent_id: str | None = None) -> dict | None:
        with self._locked(run_id, exclusive=False) as path:
            row = self._read(path, run_id)
        if row is None or row["session_id"] != session_id:
            return None
        if agent_id is not None and row["agent_id"] != agent_id:
            return None
        return row

    def cleanup_expired(self, ttl_seconds: float, *, now: float | None = None) -> int:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        cutoff = (time.time() if now is None else now) - ttl_seconds
        removed = 0
        for path in self.root.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as source:
                    run_id = json.load(source)["run_id"]
                with self._locked(run_id, exclusive=True) as locked_path:
                    row = self._read(locked_path, run_id)
                    if row and row.get("created_at", float("inf")) < cutoff:
                        locked_path.unlink()
                        removed += 1
            except FileNotFoundError:
                continue
        return removed
