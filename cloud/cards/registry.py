"""cloud.cards.registry — CardRegistry：维护运行时 Agent Card 状态。

以 module-level 单例形式暴露，供 api/devices.py 和 api/main.py 共享同一实例。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy

from cloud.cards.builder import build_card_from_manifest
from cloud.cards.validation import normalize_card
from cloud.cards.schema import AgentCard, DeviceSnapshot

logger = logging.getLogger(__name__)


class CardRegistry:
    """运行时 Agent Card 注册表。

    内部以 unit_id 为 key 维护 AgentCard 字典。
    宿主进程在 MQTT 回调中调用 handle_message 即可保持注册表最新。
    线程安全：MQTT 回调线程写，FastAPI 请求线程读，均通过 _lock 保护。
    """

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._cards: dict[str, AgentCard] = {}
        self._states: dict[str, dict] = {}
        self._retired_epochs: dict[str, list[str]] = {}
        self._statuses: dict[str, bool] = {}
        self._status_seen_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def subscribe(self, client) -> None:
        """在 MQTT on_connect 回调中调用，订阅 card 与 manifest topic。

        card topic：自描述设备（Go2 等）发布完整 AgentCard JSON。
        manifest topic：ESP32 设备发布 manifest，由 builder 组装成 AgentCard。
        """
        client.subscribe([
            ("ssm/agents/+/card",     0),
            ("ssm/agents/+/manifest", 0),
            ("ssm/agents/+/status",   0),
        ])
        logger.info("[CardRegistry] Subscribed to card / manifest / status")

    def handle_message(self, topic: str, payload: bytes | str, *,
                       retained: bool = False, received_at: float | None = None) -> None:
        """处理 MQTT 消息，更新注册表及最近一次设备状态。

        topic 匹配 ssm/agents/+/status：
            - online/offline → 保存 unit_id/device_id 的在线事实，读取快照时合成
        topic 匹配 ssm/agents/+/card：
            - 空 payload → 移除该 card（设备离线）
            - 非空 payload → normalize_card 后存入
        topic 匹配 ssm/agents/+/manifest：
            - 空 payload → 移除该单元 card（单元缺席）
            - 非空 payload → build_card_from_manifest → 以 card.unit_id 存入静态声明
        topic 匹配 ssm/agents/+/state：保存 retained 快照供设备列表读取。
        其他 topic：静默忽略。
        """
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != "ssm" or parts[1] != "agents":
            return

        msg_type = parts[3]
        raw = payload if isinstance(payload, bytes) else payload.encode()

        if msg_type == "state":
            try:
                state = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return
            if not isinstance(state, dict):
                return
            state.pop("_received_at", None)
            if not retained:
                state["_received_at"] = time.time() if received_at is None else received_at
            unit_id = parts[2]
            with self._lock:
                previous = self._states.get(unit_id, {})
                if state.get("state_schema_version") == 2:
                    epoch, revision = state.get("epoch"), state.get("revision")
                    if not isinstance(epoch, str) or not epoch or type(revision) is not int or revision < 0:
                        return
                    if epoch in self._retired_epochs.get(unit_id, ()):
                        return
                    if previous.get("state_schema_version") == 2 and previous.get("epoch") == epoch:
                        previous_revision = previous.get("revision", -1)
                        if revision < previous_revision:
                            return
                        if revision == previous_revision:
                            # Stable state is periodically published as a heartbeat.
                            # Equal versions may refresh freshness, never change facts.
                            ignored = {"ts", "_received_at"}
                            facts = lambda item: {key: value for key, value in item.items() if key not in ignored}
                            if retained or facts(previous) != facts(state):
                                return
                            old_ts, new_ts = previous.get("ts"), state.get("ts")
                            if (type(old_ts) in (int, float) and
                                    (type(new_ts) not in (int, float) or new_ts < old_ts)):
                                return
                    old_epoch = previous.get("epoch")
                    if isinstance(old_epoch, str) and old_epoch != epoch:
                        retired = self._retired_epochs.setdefault(unit_id, [])
                        retired.append(old_epoch)
                        del retired[:-16]
                elif previous.get("state_schema_version") == 2:
                    # An unordered message cannot overwrite a versioned snapshot.
                    return
                self._states[unit_id] = state
            return

        # status：设备在线事实（含 LWT），以 unit_id/device_id 关联
        if msg_type == "status":
            device_id = parts[2]
            online = (raw.strip() == b"online")
            with self._lock:
                self._statuses[device_id] = online
                self._status_seen_at[device_id] = time.time()
            logger.info("[CardRegistry] %s → online=%s", device_id, online)
            return

        if msg_type == "card":
            unit_id = parts[2]
            if not raw.strip():
                with self._lock:
                    if unit_id in self._cards:
                        del self._cards[unit_id]
                        logger.info("[CardRegistry] Removed card: %s", unit_id)
                    self._states.pop(unit_id, None)
                    self._retired_epochs.pop(unit_id, None)
            else:
                try:
                    data = json.loads(raw)
                    if data.get("agent_type") == "sensor":
                        with self._lock:
                            self._cards.pop(unit_id, None)
                        return
                    card = normalize_card(data)
                    if card["unit_id"] != unit_id:
                        raise ValueError("card unit_id does not match MQTT topic")
                    with self._lock:
                        self._cards[card["unit_id"]] = card
                    logger.info("[CardRegistry] Stored card (self-described): %s", card["unit_id"])
                except Exception as exc:
                    logger.warning("[CardRegistry] Failed to parse card payload: %s", exc)

        elif msg_type == "manifest":
            unit_id = parts[2]
            # 空 manifest = 该单元缺席，移除对应 card
            if not raw.strip():
                with self._lock:
                    if unit_id in self._cards:
                        del self._cards[unit_id]
                        logger.info("[CardRegistry] Removed card (manifest cleared): %s", unit_id)
                    self._states.pop(unit_id, None)
                return
            try:
                data = json.loads(raw)
                # 传感器不是独立智能体；云端智能体只发布自描述 Card。
                if data.get("agent_type") == "decision":
                    return
                if data.get("agent_type") == "sensor":
                    with self._lock:
                        self._cards.pop(unit_id, None)
                    return
                if data.get("hw_platform") == "cloud":
                    return
                card = build_card_from_manifest(data)
                if card["unit_id"] != unit_id:
                    raise ValueError("manifest unit_id does not match MQTT topic")
                with self._lock:
                    self._cards[card["unit_id"]] = card
                logger.info("[CardRegistry] Stored card (from manifest): %s", card["unit_id"])
            except Exception as exc:
                logger.warning("[CardRegistry] Failed to build card from manifest: %s", exc)

    def _device_snapshot(self, card: AgentCard) -> DeviceSnapshot:
        """Compose runtime facts without modifying the retained declaration; lock held."""
        unit_id = card["unit_id"]
        return {**deepcopy(card),
                "online": self._statuses.get(card["device_id"], self._statuses.get(unit_id, False)),
                "state": deepcopy(self._states.get(unit_id, {}))}

    def get_all_cards(self) -> dict[str, DeviceSnapshot]:
        with self._lock:
            return {unit_id: self._device_snapshot(card) for unit_id, card in self._cards.items()}

    def get_card(self, unit_id: str) -> DeviceSnapshot | None:
        with self._lock:
            card = self._cards.get(unit_id)
            return self._device_snapshot(card) if card is not None else None

    def get_static_card(self, unit_id: str) -> AgentCard | None:
        """Return the original static declaration without state/status projection."""
        with self._lock:
            card = self._cards.get(unit_id)
            return deepcopy(card) if card is not None else None

    def get_runtime_snapshot(self) -> tuple[dict[str, DeviceSnapshot], dict[str, float]]:
        with self._lock:
            return ({unit_id: self._device_snapshot(card) for unit_id, card in self._cards.items()},
                    dict(self._status_seen_at))


# module-level 单例，供所有模块共享同一实例
_registry = CardRegistry()


def get_registry() -> CardRegistry:
    """返回全局 CardRegistry 单例。"""
    return _registry
