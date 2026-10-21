"""Persistent pairing labels for physical devices supported by onboarding."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from pathlib import Path

SUPPORTED_DEVICES = frozenset({"esp32_desk", "go2"})
PREFIX = "ssm-device:v1"
_DATA_FILE = Path(__file__).with_name("device_codes.json")


class DeviceCodeStore:
    def __init__(self, path: Path = _DATA_FILE) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}

    def _save(self, data: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def get(self, device_id: str, *, rotate: bool = False) -> dict[str, object]:
        if device_id not in SUPPORTED_DEVICES:
            raise ValueError("unsupported device id")
        with self._lock:
            data = self._load()
            entry = data.get(device_id)
            if rotate or not isinstance(entry, dict) or not entry.get("token"):
                version = int(entry.get("version", 0)) + 1 if isinstance(entry, dict) else 1
                entry = {"version": version, "token": secrets.token_urlsafe(32)}
                data[device_id] = entry
                self._save(data)
            return {
                "device_id": device_id,
                "version": int(entry["version"]),
                "code": f"{PREFIX}:{device_id}:{entry['token']}",
            }

    def validate(self, code: str) -> str | None:
        parts = code.split(":") if isinstance(code, str) else []
        if len(parts) != 4 or parts[:2] != ["ssm-device", "v1"]:
            return None
        device_id, token = parts[2:]
        if device_id not in SUPPORTED_DEVICES or not token:
            return None
        with self._lock:
            entry = self._load().get(device_id)
            if not isinstance(entry, dict):
                return None
            expected = entry.get("token")
            if not isinstance(expected, str) or not hmac.compare_digest(token, expected):
                return None
        return device_id


_store = DeviceCodeStore()


def get_device_code_store() -> DeviceCodeStore:
    return _store
