"""Single-space configuration and device/card projection.

``space.json`` is the only runtime source for space membership. The legacy
``devices.json`` is intentionally left untouched as a rollback artefact.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_SPACE_ID = "main"
_DATA_FILE = Path(__file__).with_name("space.json")
_DEFAULT_SPACE = {
    "version": 1,
    "id": DEFAULT_SPACE_ID,
    "name": "SSM 体验空间",
    "device_ids": ["esp32_desk", "go2"],
    "invite": {"enabled": False, "token_hash": "", "version": 0, "updated_at": 0},
    "updated_at": 0,
}


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def card_device_id(card: dict[str, Any]) -> str:
    return str(card.get("device_id") or card.get("parent_id") or card.get("unit_id") or "")


def group_cards_by_device(cards: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for card in cards.values():
        device_id = card_device_id(card)
        if not device_id or not card.get("unit_id"):
            continue
        item = dict(card)
        item["device_id"] = device_id
        groups.setdefault(device_id, []).append(item)
    for group in groups.values():
        group.sort(key=lambda item: str(item.get("unit_id", "")))
    return groups


def _offline_card(device_id: str) -> dict[str, Any]:
    if device_id == "go2":
        return {
            "unit_id": "go2", "device_id": "go2", "name": "Go2 机器狗",
            "description": "四足机器人（离线）", "agent_type": "robot", "online": False,
            "transport": {"kind": "http"}, "skills": [], "state": {},
        }
    if device_id == "esp32_desk":
        return {
            "unit_id": "esp32_desk", "device_id": "esp32_desk", "name": "ESP32 桌面空间",
            "description": "桌面灯光与传感器（离线）", "agent_type": "actuator", "online": False,
            "transport": {"kind": "mqtt"}, "skills": [], "state": {},
        }
    return {
        "unit_id": device_id, "device_id": device_id, "name": device_id,
        "description": "设备离线", "agent_type": "device", "online": False,
        "transport": {}, "skills": [], "state": {},
    }


class SpaceRegistry:
    """Thread-safe store for the one fixed space."""

    def __init__(self, path: Path = _DATA_FILE) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._ensure_file()

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_file(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            data = dict(_DEFAULT_SPACE)
            data["invite"] = dict(_DEFAULT_SPACE["invite"])
            data["updated_at"] = int(time.time())
            self._atomic_save(data)

    def _normalize(self, raw: Any) -> dict[str, Any]:
        data = raw if isinstance(raw, dict) else {}
        invite = data.get("invite") if isinstance(data.get("invite"), dict) else {}
        return {
            "version": 1,
            "id": DEFAULT_SPACE_ID,
            "name": str(data.get("name") or _DEFAULT_SPACE["name"]),
            "device_ids": _unique_strings(data.get("device_ids") or _DEFAULT_SPACE["device_ids"]),
            "invite": {
                "enabled": bool(invite.get("enabled", False)),
                "token_hash": str(invite.get("token_hash") or ""),
                "version": max(0, int(invite.get("version") or 0)),
                "updated_at": int(invite.get("updated_at") or 0),
            },
            "updated_at": int(data.get("updated_at") or 0),
        }

    def _load_unlocked(self) -> dict[str, Any]:
        self._ensure_file()
        try:
            return self._normalize(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return self._normalize({})

    def _atomic_save(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)

    def get_space(self) -> dict[str, Any]:
        with self._lock:
            data = self._load_unlocked()
            data["invite"] = dict(data["invite"])
            data["device_ids"] = list(data["device_ids"])
            return data

    def update_space(self, *, name: str | None = None, device_ids: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            data = self._load_unlocked()
            if name is not None:
                clean_name = name.strip()
                if not clean_name:
                    raise ValueError("name must not be empty")
                data["name"] = clean_name
            if device_ids is not None:
                clean_ids = _unique_strings(device_ids)
                if not clean_ids:
                    raise ValueError("device_ids must not be empty")
                if not {"esp32_desk", "go2"}.issubset(clean_ids):
                    raise ValueError("the main space must include esp32_desk and go2")
                data["device_ids"] = clean_ids
            data["updated_at"] = int(time.time())
            self._atomic_save(data)
            return self.get_space()

    def rotate_invite(self, token_hash: str) -> dict[str, Any]:
        with self._lock:
            data = self._load_unlocked()
            now = int(time.time())
            invite = data["invite"]
            invite.update({
                "enabled": True, "token_hash": token_hash,
                "version": int(invite["version"]) + 1, "updated_at": now,
            })
            data["updated_at"] = now
            self._atomic_save(data)
            return dict(invite)

    def disable_invite(self) -> dict[str, Any]:
        with self._lock:
            data = self._load_unlocked()
            now = int(time.time())
            invite = data["invite"]
            invite.update({
                "enabled": False, "token_hash": "",
                "version": int(invite["version"]) + 1, "updated_at": now,
            })
            data["updated_at"] = now
            self._atomic_save(data)
            return dict(invite)

    # Compatibility for read-only orchestration code during migration. No
    # caller-supplied space id is accepted or consulted.
    def list_adoptions(self) -> dict[str, dict[str, Any]]:
        return {
            device_id: {"device_id": device_id, "unit_ids": []}
            for device_id in self.get_space()["device_ids"]
        }


def get_space_cards(cards: dict[str, dict[str, Any]], registry: SpaceRegistry | None = None) -> dict[str, dict[str, Any]]:
    registry = registry or get_space_registry()
    groups = group_cards_by_device(cards)
    result: dict[str, dict[str, Any]] = {}
    for device_id in registry.get_space()["device_ids"]:
        discovered = groups.get(device_id, [])
        if discovered:
            for card in discovered:
                result[str(card["unit_id"])] = card
        else:
            placeholder = _offline_card(device_id)
            result[str(placeholder["unit_id"])] = placeholder
    return result


def get_adopted_cards(
    cards: dict[str, dict[str, Any]], registry: SpaceRegistry | None = None, *_legacy: Any
) -> dict[str, dict[str, Any]]:
    """Deprecated name retained for read-only callers; always projects ``main``."""
    return get_space_cards(cards, registry)


def build_adoption_candidates(
    cards: dict[str, dict[str, Any]], registry: SpaceRegistry | None = None, *_legacy: Any, **_kwargs: Any
) -> list[dict[str, Any]]:
    """Deprecated discovery helper; candidates are never adopted by this API."""
    registry = registry or get_space_registry()
    configured = set(registry.get_space()["device_ids"])
    return [
        {"device_id": device_id, "cards": group, "online": any(c.get("online", False) for c in group)}
        for device_id, group in sorted(group_cards_by_device(cards).items())
        if device_id not in configured
    ]


_registry = SpaceRegistry()


def get_space_registry() -> SpaceRegistry:
    return _registry
