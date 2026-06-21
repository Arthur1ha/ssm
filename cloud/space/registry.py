"""cloud.space.registry — 当前空间的设备接入状态。

CardRegistry 负责“系统发现了哪些 Agent Card”；SpaceRegistry 只负责
“当前空间接入了哪些 device_id”。设备能力始终从 Agent Card 动态读取，
这里不保存技能、状态机或具体设备类型，避免形成第二份能力真相。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_SPACE_ID = "default"
_DATA_FILE = Path(__file__).with_name("devices.json")
_DEFAULT_PERMISSIONS = {
    "allow_control": True,
    "allow_autonomy": True,
    "allow_rules": True,
}


def card_device_id(card: dict[str, Any]) -> str:
    """返回 card 所属的物理设备/节点 ID，不引入额外接入 ID。"""
    return str(card.get("device_id") or card.get("parent_id") or card.get("unit_id") or "")


def _unique(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def group_cards_by_device(cards: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按 device_id / parent_id / unit_id 聚合 Agent Cards。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for card in cards.values():
        device_id = card_device_id(card)
        unit_id = card.get("unit_id")
        if not device_id or not unit_id:
            continue
        item = dict(card)
        item["device_id"] = device_id
        groups.setdefault(device_id, []).append(item)
    for items in groups.values():
        items.sort(key=lambda c: str(c.get("unit_id", "")))
    return groups


def _primary_card(cards: list[dict[str, Any]]) -> dict[str, Any]:
    for kind in ("robot", "actuator"):
        for card in cards:
            if card.get("agent_type") == kind:
                return card
    return cards[0] if cards else {}


def _candidate_name(device_id: str, cards: list[dict[str, Any]]) -> str:
    primary = _primary_card(cards)
    base = primary.get("device_name") or primary.get("name") or device_id
    if len(cards) <= 1:
        return str(base)
    return f"{base} 等 {len(cards)} 个成员"


def _candidate_summary(cards: list[dict[str, Any]]) -> str:
    names = _unique([str(c.get("name") or c.get("unit_id") or "") for c in cards])
    if len(names) > 1:
        return "包含：" + "、".join(names[:4]) + ("…" if len(names) > 4 else "")
    descriptions = _unique([str(c.get("description") or "") for c in cards])
    if descriptions:
        return descriptions[0]
    skills = _unique([
        str(skill.get("name") or skill.get("id") or "")
        for card in cards
        for skill in card.get("skills", [])
    ])
    if skills:
        return "能力：" + "、".join(skills[:4]) + ("…" if len(skills) > 4 else "")
    return "已发布能力声明"


def build_device_candidate(device_id: str, cards: list[dict[str, Any]]) -> dict[str, Any]:
    """从 Agent Card 动态生成接入候选卡数据。"""
    primary = _primary_card(cards)
    unit_ids = _unique([str(c.get("unit_id") or "") for c in cards])
    skills = _unique([
        str(skill.get("name") or skill.get("id") or "")
        for card in cards
        for skill in card.get("skills", [])
    ])
    sensors = _unique([
        str(card.get("name") or card.get("unit_id") or "")
        for card in cards
        if card.get("agent_type") == "sensor"
    ])
    transport_kinds = _unique([
        str((card.get("transport") or {}).get("kind") or "")
        for card in cards
    ])
    online = any(card.get("online", True) for card in cards)
    return {
        "device_id": device_id,
        "name": _candidate_name(device_id, cards),
        "online": online,
        "agent_type": "device_node" if len(cards) > 1 else primary.get("agent_type", "device"),
        "summary": _candidate_summary(cards),
        "transport_kinds": transport_kinds,
        "unit_ids": unit_ids,
        "skills": skills,
        "sensors": sensors,
        "cards": cards,
    }


def build_adoption_candidates(
    cards: dict[str, dict[str, Any]],
    registry: "SpaceRegistry",
    space_id: str = DEFAULT_SPACE_ID,
) -> list[dict[str, Any]]:
    adopted = set(registry.list_adoptions(space_id).keys())
    groups = group_cards_by_device(cards)
    return [
        build_device_candidate(device_id, items)
        for device_id, items in sorted(groups.items())
        if device_id not in adopted
    ]


def get_adopted_cards(
    cards: dict[str, dict[str, Any]],
    registry: "SpaceRegistry",
    space_id: str = DEFAULT_SPACE_ID,
) -> dict[str, dict[str, Any]]:
    adoptions = registry.list_adoptions(space_id)
    adopted_device_ids = set(adoptions.keys())
    return {
        uid: card
        for uid, card in cards.items()
        if card_device_id(card) in adopted_device_ids
    }


class SpaceRegistry:
    """文件型空间接入注册表，供 API 与编排器进程共享。"""

    def __init__(self, path: Path = _DATA_FILE) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._ensure_file()

    def _ensure_file(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text(json.dumps({DEFAULT_SPACE_ID: {}}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        self._ensure_file()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {DEFAULT_SPACE_ID: {}}
        except Exception:
            return {DEFAULT_SPACE_ID: {}}

    def _save(self, data: dict[str, dict[str, dict[str, Any]]]) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def list_adoptions(self, space_id: str = DEFAULT_SPACE_ID) -> dict[str, dict[str, Any]]:
        with self._lock:
            data = self._load()
            return dict(data.get(space_id, {}))

    def adopt(
        self,
        device_id: str,
        unit_ids: list[str],
        display_name: str = "",
        location: str = "",
        permissions: dict[str, Any] | None = None,
        space_id: str = DEFAULT_SPACE_ID,
    ) -> dict[str, Any]:
        if not device_id:
            raise ValueError("device_id is required")
        clean_units = _unique(unit_ids)
        if not clean_units:
            raise ValueError("unit_ids is required")
        perms = dict(_DEFAULT_PERMISSIONS)
        if permissions:
            perms.update(permissions)
        record = {
            "device_id": device_id,
            "unit_ids": clean_units,
            "display_name": display_name or device_id,
            "location": location or "",
            "permissions": perms,
            "created_at": int(time.time()),
        }
        with self._lock:
            data = self._load()
            data.setdefault(space_id, {})[device_id] = record
            self._save(data)
        return record

    def remove(self, device_id: str, space_id: str = DEFAULT_SPACE_ID) -> bool:
        with self._lock:
            data = self._load()
            bucket = data.setdefault(space_id, {})
            existed = device_id in bucket
            if existed:
                del bucket[device_id]
                self._save(data)
            return existed


_registry = SpaceRegistry()


def get_space_registry() -> SpaceRegistry:
    return _registry
