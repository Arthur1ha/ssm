"""cloud.api.devices — /api/devices 相关端点。

设备列表和 Agent Card 数据来自 CardRegistry，
Registry 由 api/main.py 的 MQTT 回调维护。
"""

from fastapi import APIRouter, HTTPException

from cloud.cards.registry import get_registry

router = APIRouter()


@router.get("/api/devices")
def list_devices():
    """返回所有已注册设备的精简列表（slug/name/agent_type/online）。

    数据来源为 CardRegistry，仅包含已收到 manifest 或自描述 card 的设备。
    Go2 在 Task 2 接入前不会出现在此列表。
    """
    cards = get_registry().get_all_cards()
    return [
        {
            "unit_id":    card.get("slug"),   # registry 以 slug 索引，unit_id 对外沿用 slug
            "name":       card.get("name"),
            "slug":       card.get("slug"),
            "agent_type": card.get("agent_type"),
            "online":     card.get("online", False),
        }
        for card in cards.values()
    ]


@router.get("/api/devices/{slug}/agent")
def device_agent_card(slug: str):
    """返回指定设备的完整 Agent Card（机器可读能力描述）。

    供其他 AI Agent 或 PWA 自动发现和调用设备能力。
    Go2 在 Task 2 接入前会返回 404。
    """
    card = get_registry().get_card(slug)
    if card is None:
        raise HTTPException(status_code=404, detail=f"设备 '{slug}' 不存在或未上线")
    return card
