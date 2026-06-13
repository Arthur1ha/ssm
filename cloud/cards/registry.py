"""cloud.cards.registry — CardRegistry：维护运行时 Agent Card 状态。

以 module-level 单例形式暴露，供 api/devices.py 和 api/main.py 共享同一实例。
"""

from __future__ import annotations

import json
import logging
import threading

from cloud.cards.builder import build_card_from_manifest, parse_card
from cloud.cards.schema import AgentCard

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

    def handle_message(self, topic: str, payload: bytes | str) -> None:
        """处理 MQTT 消息，更新注册表。

        topic 匹配 ssm/agents/+/status：
            - online/offline → 按 unit_id 或 parent_id 匹配，维护 card.online（含 LWT）
        topic 匹配 ssm/agents/+/card：
            - 空 payload → 移除该 card（设备离线）
            - 非空 payload → parse_card 后存入
        topic 匹配 ssm/agents/+/manifest：
            - 空 payload → 移除该单元 card（单元缺席）
            - 非空 payload → build_card_from_manifest → 以 card.unit_id 存入（保留已知 online）
        其他 topic：静默忽略。
        """
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != "ssm" or parts[1] != "agents":
            return

        msg_type = parts[3]
        raw = payload if isinstance(payload, bytes) else payload.encode()

        # status：父设备/设备级在线状态（含 LWT），按 unit_id 或 parent_id 匹配
        if msg_type == "status":
            device_id = parts[2]
            online = (raw.strip() == b"online")
            with self._lock:
                for c in self._cards.values():
                    if c.get("unit_id") == device_id or c.get("parent_id") == device_id:
                        c["online"] = online
            logger.info("[CardRegistry] %s → online=%s", device_id, online)
            return

        if msg_type == "card":
            unit_id = parts[2]
            if not raw.strip():
                with self._lock:
                    if unit_id in self._cards:
                        del self._cards[unit_id]
                        logger.info("[CardRegistry] Removed card: %s", unit_id)
            else:
                try:
                    data = json.loads(raw)
                    card = parse_card(data)
                    with self._lock:
                        # 保留已知 online（status 可能先于 card 到达，且是在线真相）
                        prev = self._cards.get(card["unit_id"])
                        if prev is not None and "online" in prev:
                            card["online"] = prev["online"]
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
                return
            try:
                data = json.loads(raw)
                # 系统/监控节点不参与编排
                if data.get("agent_type") in ("supervisor", "decision"):
                    return
                if data.get("hw_platform") in ("pwa", "pc"):
                    return
                card = build_card_from_manifest(data)
                with self._lock:
                    # 保留已知 online（status 可能先于 manifest 到达，且是在线真相）
                    prev = self._cards.get(card["unit_id"])
                    if prev is not None and "online" in prev:
                        card["online"] = prev["online"]
                    self._cards[card["unit_id"]] = card
                logger.info("[CardRegistry] Stored card (from manifest): %s", card["unit_id"])
            except Exception as exc:
                logger.warning("[CardRegistry] Failed to build card from manifest: %s", exc)

    def get_all_cards(self) -> dict[str, AgentCard]:
        """返回当前注册表的浅拷贝，防止调用方修改内部状态。"""
        with self._lock:
            return dict(self._cards)

    def get_card(self, unit_id: str) -> AgentCard | None:
        """按 unit_id 查找 AgentCard，返回副本以防止调用方修改内部状态。"""
        with self._lock:
            card = self._cards.get(unit_id)
            return dict(card) if card is not None else None


# module-level 单例，供所有模块共享同一实例
_registry = CardRegistry()


def get_registry() -> CardRegistry:
    """返回全局 CardRegistry 单例。"""
    return _registry
