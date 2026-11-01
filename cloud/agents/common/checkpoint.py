"""JSON file backed LangGraph checkpoints shared by the API and MQTT workers.

Each thread is one atomically replaced JSON document.  The lock covers both
checkpoints and pending writes, so two processes never lose a write by racing
through a read/modify/write cycle.  The typed LangGraph serializer retains
native messages and interrupt values inside that document.
"""

import asyncio
import base64
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    WRITES_IDX_MAP,
    get_checkpoint_id,
    get_checkpoint_metadata,
    writes_sort_key,
)

from cloud.observability import current_event_context, emit_agent_event


def _default_root() -> Path:
    return Path(os.getenv("SSM_CHECKPOINT_DIR", Path(__file__).resolve().parents[2] / "data" / "checkpoints"))


class FileCheckpointSaver(BaseCheckpointSaver):
    """A synchronous and asynchronous BaseCheckpointSaver with JSON files.

    Run Graphs with ``durability='sync'`` when a physical tool may execute.
    The caller must still serialize active runs per thread and reconcile a
    previously dispatched task ID before replaying any physical tool.
    """

    def __init__(self, root: str | Path | None = None, *, serde=None) -> None:
        super().__init__(serde=serde)
        self.root = Path(root) if root is not None else _default_root()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _paths(self, thread_id: str) -> tuple[Path, Path]:
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id is required")
        name = sha256(thread_id.encode()).hexdigest()
        return self.root / f"{name}.json", self.root / f"{name}.lock"

    def _run_lock_path(self, thread_id: str) -> Path:
        path, _ = self._paths(thread_id)
        return path.with_suffix(".run.lock")

    @contextmanager
    def run_lock(self, thread_id: str):
        """Serialize whole Graph runs for a thread across API/MQTT processes.

        Hold this around graph invocation, including reconciliation of an
        interrupted physical task. The JSON write lock alone cannot prevent
        two independent runs from reading the same head checkpoint.
        """
        fd = os.open(self._run_lock_path(thread_id), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @asynccontextmanager
    async def async_run_lock(self, thread_id: str):
        """Nonblocking event-loop variant for ``async with`` graph runs."""
        fd = os.open(self._run_lock_path(thread_id), os.O_CREAT | os.O_RDWR, 0o600)
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

    def cleanup_expired(self, ttl_seconds: float, *, now: float | None = None) -> int:
        """Delete idle threads whose newest checkpoint is older than TTL.

        An active run holds ``run_lock``; cleanup skips it without waiting.
        Callers should invoke this periodically from a maintenance worker.
        """
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be nonnegative")
        cutoff = (time.time() if now is None else now) - ttl_seconds
        removed = 0
        for path in self.root.glob("*.json"):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                with path.open("r", encoding="utf-8") as source:
                    thread_id = json.load(source)["thread_id"]
                fd = os.open(self._run_lock_path(thread_id), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    if path.exists() and path.stat().st_mtime < cutoff:
                        self.delete_thread(thread_id)
                        removed += 1
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
            except FileNotFoundError:
                continue
        return removed

    @contextmanager
    def _locked(self, thread_id: str, *, exclusive: bool):
        path, lock_path = self._paths(thread_id)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            if path.exists():
                with path.open("r", encoding="utf-8") as source:
                    data = json.load(source)
                if data.get("thread_id") != thread_id or not isinstance(data.get("namespaces"), dict):
                    raise ValueError(f"Invalid checkpoint file: {path}")
            else:
                data = {"thread_id": thread_id, "namespaces": {}}
            yield data
            if exclusive:
                self._atomic_write(path, data)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _atomic_write(self, path: Path, data: dict) -> None:
        fd, name = tempfile.mkstemp(prefix=".checkpoint-", suffix=".tmp", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(data, target, ensure_ascii=False, separators=(",", ":"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, path)
            dir_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _pack(self, value: Any) -> list[str]:
        kind, payload = self.serde.dumps_typed(value)
        return [kind, base64.b64encode(payload).decode("ascii")]

    def _unpack(self, value: list[str]) -> Any:
        return self.serde.loads_typed((value[0], base64.b64decode(value[1])))

    @staticmethod
    def _config(thread_id: str, ns: str, checkpoint_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}}

    def _tuple(self, thread_id: str, ns: str, checkpoint_id: str, row: dict) -> CheckpointTuple:
        ordered = sorted(row.get("writes", []), key=lambda write: writes_sort_key(write["path"], write["task_id"], write["index"]))
        parent = row.get("parent")
        return CheckpointTuple(
            config=self._config(thread_id, ns, checkpoint_id),
            checkpoint=self._unpack(row["checkpoint"]),
            metadata=self._unpack(row["metadata"]),
            parent_config=self._config(thread_id, ns, parent) if parent else None,
            pending_writes=[(write["task_id"], write["channel"], self._unpack(write["value"])) for write in ordered],
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        cfg = config["configurable"]
        thread_id = cfg["thread_id"]
        ns = cfg.get("checkpoint_ns", "")
        with self._locked(thread_id, exclusive=False) as data:
            rows = data["namespaces"].get(ns, {})
            checkpoint_id = get_checkpoint_id(config) or max(rows, default="")
            row = rows.get(checkpoint_id)
            return self._tuple(thread_id, ns, checkpoint_id, row) if row else None

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if limit is not None and limit <= 0:
            return
        if config:
            thread_ids = [config["configurable"]["thread_id"]]
        else:
            thread_ids = []
            for path in self.root.glob("*.json"):
                with path.open("r", encoding="utf-8") as source:
                    thread_ids.append(json.load(source)["thread_id"])
        results = []
        for thread_id in thread_ids:
            with self._locked(thread_id, exclusive=False) as data:
                for ns, rows in data["namespaces"].items():
                    if config and config["configurable"].get("checkpoint_ns") not in (None, ns):
                        continue
                    for checkpoint_id, row in rows.items():
                        if config and get_checkpoint_id(config) and checkpoint_id != get_checkpoint_id(config):
                            continue
                        if before and get_checkpoint_id(before) and checkpoint_id >= get_checkpoint_id(before):
                            continue
                        metadata = self._unpack(row["metadata"])
                        if filter and any(metadata.get(key) != value for key, value in filter.items()):
                            continue
                        results.append(self._tuple(thread_id, ns, checkpoint_id, row))
        results.sort(key=lambda item: item.config["configurable"]["checkpoint_id"], reverse=True)
        yield from results[:limit]

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        cfg = config["configurable"]
        thread_id = cfg["thread_id"]
        ns = cfg.get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        with self._locked(thread_id, exclusive=True) as data:
            rows = data["namespaces"].setdefault(ns, {})
            existing_writes = rows.get(checkpoint_id, {}).get("writes", [])
            rows[checkpoint_id] = {
                "checkpoint": self._pack(checkpoint),
                "metadata": self._pack(get_checkpoint_metadata(config, metadata)),
                "parent": cfg.get("checkpoint_id"),
                "writes": existing_writes,
            }
        if current_event_context().get("agent_id"):
            emit_agent_event("checkpoint.saved", thread_id=thread_id,
                             checkpoint_id=checkpoint_id,
                             parent_checkpoint_id=cfg.get("checkpoint_id"),
                             checkpoint_ns=ns)
        return self._config(thread_id, ns, checkpoint_id)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        cfg = config["configurable"]
        thread_id = cfg["thread_id"]
        ns = cfg.get("checkpoint_ns", "")
        checkpoint_id = cfg["checkpoint_id"]
        with self._locked(thread_id, exclusive=True) as data:
            row = data["namespaces"].setdefault(ns, {}).setdefault(checkpoint_id, {"writes": []})
            stored = row["writes"]
            for position, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, position)
                existing = next((item for item in stored if item["task_id"] == task_id and item["index"] == index), None)
                if existing and index >= 0:
                    continue
                packed = {"task_id": task_id, "path": task_path, "index": index, "channel": channel, "value": self._pack(value)}
                if existing:
                    stored.remove(existing)
                stored.append(packed)

    def delete_thread(self, thread_id: str) -> None:
        path, lock_path = self._paths(thread_id)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path.unlink(missing_ok=True)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)
