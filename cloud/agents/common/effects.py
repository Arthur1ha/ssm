"""Durable reservation ledger for physical tool calls.

Reserve a stable call ID before dispatch. A reservation without a receipt is
unknown after a crash and must be reconciled with the device task ledger;
it is never permission to dispatch the call again.
"""

import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import time


class FileEffectLedger:
    def __init__(self, root: str | Path | None = None) -> None:
        default = Path(__file__).resolve().parents[2] / "data" / "effects"
        self.root = Path(root or os.getenv("SSM_EFFECT_LEDGER_DIR", default))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    @staticmethod
    def _identity(agent_id: str, thread_id: str, run_id: str, step_id: str) -> tuple[str, ...]:
        parts = (agent_id, thread_id, run_id, step_id)
        if any(not isinstance(part, str) or not part for part in parts):
            raise ValueError("agent_id, thread_id, run_id, and step_id are required")
        return parts

    def _paths(self, identity: tuple[str, ...]) -> tuple[Path, Path]:
        name = sha256("\0".join(identity).encode()).hexdigest()
        return self.root / f"{name}.json", self.root / f"{name}.lock"

    @staticmethod
    def _fingerprint(tool: str, params: dict) -> str:
        if not isinstance(tool, str) or not tool or not isinstance(params, dict):
            raise ValueError("tool and JSON object params are required")
        payload = json.dumps({"tool": tool, "params": params}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return sha256(payload.encode()).hexdigest()

    @staticmethod
    def _view(row: dict, reservation: str | None = None) -> dict:
        view = {
            "status": "observed" if row.get("result") is not None else "unknown",
            "tool": row["tool"],
            "result": row.get("result"),
            "step_id": row["identity"][3],
        }
        if reservation:
            view["reservation"] = reservation
            if reservation == "new":
                view["status"] = "reserved"
        return view

    def _read(self, path: Path, identity: tuple[str, ...]) -> dict | None:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as source:
            row = json.load(source)
        if tuple(row.get("identity", ())) != identity or not isinstance(row.get("fingerprint"), str):
            raise ValueError(f"Invalid effect ledger record: {path}")
        return row

    def _write(self, path: Path, row: dict) -> None:
        fd, name = tempfile.mkstemp(prefix=".effect-", suffix=".tmp", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(row, target, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
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

    def reserve(self, agent_id: str, thread_id: str, run_id: str, step_id: str, tool: str, params: dict) -> dict:
        identity = self._identity(agent_id, thread_id, run_id, step_id)
        fingerprint = self._fingerprint(tool, params)
        path, lock_path = self._paths(identity)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            row = self._read(path, identity)
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("effect_call_id_conflict")
                return self._view(row, "existing")
            row = {"identity": list(identity), "tool": tool, "fingerprint": fingerprint,
                   "result": None, "reserved_at": time.time()}
            self._write(path, row)
            return self._view(row, "new")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def mark_result(self, agent_id: str, thread_id: str, run_id: str, step_id: str, result: dict) -> dict:
        identity = self._identity(agent_id, thread_id, run_id, step_id)
        if not isinstance(result, dict):
            raise ValueError("result must be a JSON object")
        path, lock_path = self._paths(identity)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            row = self._read(path, identity)
            if row is None:
                raise ValueError("effect_not_reserved")
            if row["result"] is not None and row["result"] != result:
                raise ValueError("effect_result_conflict")
            if row["result"] is None:
                row["result"] = result
                row["observed_at"] = time.time()
                self._write(path, row)
            return self._view(row)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read(self, agent_id: str, thread_id: str, run_id: str, step_id: str) -> dict | None:
        identity = self._identity(agent_id, thread_id, run_id, step_id)
        path, lock_path = self._paths(identity)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            row = self._read(path, identity)
            return self._view(row) if row is not None else None
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
