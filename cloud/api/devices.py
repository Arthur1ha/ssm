"""cloud.api.devices — /api/devices 相关端点。

设备列表和 Agent Card 数据来自 CardRegistry，
Registry 由 api/main.py 的 MQTT 回调维护。
"""

from fastapi import APIRouter, HTTPException

from cloud.cards.registry import get_registry

router = APIRouter(prefix="/api/devices", tags=["devices"])


# ── 路由 ─────────────────────────────────────────────────────────

@router.get("")
def list_devices():
    """返回所有已注册设备的精简列表（unit_id/name/agent_type/online）。

    数据来源为 CardRegistry，仅包含已收到 manifest 或自描述 card 的设备。
    Go2 在 Task 2 接入前不会出现在此列表。
    """
    cards = get_registry().get_all_cards()
    return [
        {
            "unit_id":    card.get("unit_id"),
            "name":       card.get("name"),
            "agent_type": card.get("agent_type"),
            "online":     card.get("online", False),
        }
        for card in cards.values()
    ]


@router.get("/{unit_id}/agent")
def device_agent_card(unit_id: str):
    """返回指定设备的完整 Agent Card（机器可读能力描述）。

    供其他 AI Agent 或 PWA 自动发现和调用设备能力。
    Go2 在 Task 2 接入前会返回 404。
    """
    card = get_registry().get_card(unit_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"设备 '{unit_id}' 不存在或未上线")
    return card
