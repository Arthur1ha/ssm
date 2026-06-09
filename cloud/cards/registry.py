"""cloud.cards.registry — CardRegistry：维护运行时 Agent Card 状态。

以 module-level 单例形式暴露，供 api/devices.py 和 api/main.py 共享同一实例。
"""

from __future__ import annotations

import json
import logging

from cloud.cards.builder import build_card_from_manifest, parse_card
from cloud.cards.schema import AgentCard

logger = logging.getLogger(__name__)


class CardRegistry:
    """运行时 Agent Card 注册表。

    内部以 slug 为 key 维护 AgentCard 字典。
    宿主进程在 MQTT 回调中调用 handle_message 即可保持注册表最新。
    """

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._cards: dict[str, AgentCard] = {}

    def subscribe(self, client) -> None:
        """在 MQTT on_connect 回调中调用，订阅 card 与 manifest topic。

        card topic：自描述设备（Go2 等）发布完整 AgentCard JSON。
        manifest topic：ESP32 设备发布 manifest，由 builder 组装成 AgentCard。
        """
        client.subscribe([
            ("ssm/agents/+/card",     0),
            ("ssm/agents/+/manifest", 0),
        ])
        logger.info("[CardRegistry] Subscribed to ssm/agents/+/card and ssm/agents/+/manifest")

    def handle_message(self, topic: str, payload: bytes | str) -> None:
        """处理 MQTT 消息，更新注册表。

        topic 匹配 ssm/agents/+/card：
            - 空 payload → 移除该 slug（设备离线）
            - 非空 payload → parse_card 后存入
        topic 匹配 ssm/agents/+/manifest：
            - json parse → build_card_from_manifest → 以 card.slug 存入
        其他 topic：静默忽略。
        """
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != "ssm" or parts[1] != "agents":
            return

        msg_type = parts[3]
        raw = payload if isinstance(payload, bytes) else payload.encode()

        if msg_type == "card":
            if not raw.strip():
                # 空 payload 表示设备离线，从注册表移除
                unit_id = parts[2]
                removed = [s for s, c in self._cards.items() if c.get("slug") == unit_id or s == unit_id]
                for slug in removed:
                    del self._cards[slug]
                    logger.info("[CardRegistry] Removed card: %s", slug)
            else:
                try:
                    data = json.loads(raw)
                    card = parse_card(data)
                    self._cards[card["slug"]] = card
                    logger.info("[CardRegistry] Stored card (self-described): %s", card["slug"])
                except Exception as exc:
                    logger.warning("[CardRegistry] Failed to parse card payload: %s", exc)

        elif msg_type == "manifest":
            if not raw.strip():
                return
            try:
                data = json.loads(raw)
                card = build_card_from_manifest(data)
                self._cards[card["slug"]] = card
                logger.info("[CardRegistry] Stored card (from manifest): %s", card["slug"])
            except Exception as exc:
                logger.warning("[CardRegistry] Failed to build card from manifest: %s", exc)

    def get_all_cards(self) -> dict[str, AgentCard]:
        """返回当前注册表的浅拷贝，防止调用方修改内部状态。"""
        return dict(self._cards)

    def get_card(self, slug: str) -> AgentCard | None:
        """按 slug 查找 AgentCard，不存在返回 None。"""
        return self._cards.get(slug)


# module-level 单例，供所有模块共享同一实例
_registry = CardRegistry()


def get_registry() -> CardRegistry:
    """返回全局 CardRegistry 单例。"""
    return _registry
