"""cloud.api.devices — /api/devices 相关端点。

设备列表和 Agent Card 数据来自 CardRegistry，
Registry 由 api/main.py 的 MQTT 回调维护。
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from cloud.cards.registry import get_registry
from cloud.space.registry import (
    DEFAULT_SPACE_ID,
    build_adoption_candidates,
    build_device_candidate,
    get_adopted_cards,
    get_space_registry,
    group_cards_by_device,
)

router = APIRouter(prefix="/api/devices", tags=["devices"])


class AdoptDeviceRequest(BaseModel):
    device_id: str = Field(..., min_length=1)
    display_name: str = ""
    location: str = ""
    permissions: dict = Field(default_factory=dict)
    space_id: str = DEFAULT_SPACE_ID


def _card_response(card: dict) -> dict:
    """返回 PWA 可用的 card 摘要，保留声明式 UI 所需字段。"""
    keys = (
        "unit_id", "device_id", "parent_id", "name", "description", "agent_type",
        "online", "transport", "state_machine", "skills", "modes", "telemetry",
        "widgets", "suggestions", "tags",
    )
    return {key: card.get(key) for key in keys if key in card}


# ── 路由 ─────────────────────────────────────────────────────────

@router.get("")
def list_devices(
    scope: str = Query("discovered"),
    space_id: str = DEFAULT_SPACE_ID,
):
    """返回设备列表。

    scope=discovered：所有已收到 manifest/card 的设备（兼容旧版默认行为）。
    scope=adopted：当前空间已接入设备，供 V2 主页使用。
    """
    if scope not in ("discovered", "adopted"):
        raise HTTPException(status_code=400, detail="scope must be discovered or adopted")
    cards = get_registry().get_all_cards()
    if scope == "adopted":
        cards = get_adopted_cards(cards, get_space_registry(), space_id)
    return [_card_response(card) for card in cards.values()]


@router.get("/candidates")
def list_device_candidates(
    space_id: str = DEFAULT_SPACE_ID,
    include_offline: bool = False,
):
    """返回当前空间尚未接入的设备候选卡。"""
    cards = get_registry().get_all_cards()
    return build_adoption_candidates(cards, get_space_registry(), space_id, include_offline=include_offline)


@router.post("/adoptions")
def adopt_device(req: AdoptDeviceRequest):
    """把一个 discovered device_id 接入当前空间。"""
    cards = get_registry().get_all_cards()
    groups = group_cards_by_device(cards)
    items = groups.get(req.device_id)
    if not items:
        raise HTTPException(status_code=404, detail=f"设备 '{req.device_id}' 不存在或未上线")
    candidate = build_device_candidate(req.device_id, items)
    record = get_space_registry().adopt(
        device_id=req.device_id,
        unit_ids=candidate["unit_ids"],
        display_name=req.display_name or candidate["name"],
        location=req.location,
        permissions=req.permissions,
        space_id=req.space_id,
    )
    return {"ok": True, "device": record, "candidate": candidate}


@router.delete("/adoptions/{device_id}")
def remove_adoption(device_id: str, space_id: str = DEFAULT_SPACE_ID):
    """从当前空间移除一个已接入设备。"""
    removed = get_space_registry().remove(device_id, space_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"设备 '{device_id}' 尚未接入")
    return {"ok": True, "device_id": device_id}


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
