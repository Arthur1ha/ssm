"""Space sessions, invitations, and administration endpoints."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from cloud.api.auth import (
    check_rate_limit,
    clear_session_cookies,
    new_session,
    public_session,
    require_admin,
    require_admin_csrf,
    require_csrf,
    require_session,
    set_session_cookies,
)
from cloud.api.policy import audit_event
from cloud.cards.registry import get_registry
from cloud.space.registry import get_space_registry, group_cards_by_device
from cloud.space.device_codes import get_device_code_store, SUPPORTED_DEVICES

router = APIRouter(tags=["sessions"])


class VisitorSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(..., min_length=16, max_length=512)


class DeviceBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., min_length=1, max_length=512)


class SpacePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=80)
    device_ids: list[str] | None = None


@router.post("/api/admin/session")
async def create_admin_session(request: Request, response: Response):
    check_rate_limit(request, "admin-session")
    session = new_session("admin")
    set_session_cookies(response, session)
    from cloud.api.intents import schedule_session_cleanup
    schedule_session_cleanup(session)
    audit_event("admin_session_created", session)
    return public_session(session)


@router.get("/api/admin/device-codes/{device_id}")
async def get_device_code(device_id: str, _session: dict[str, Any] = Depends(require_admin)):
    if device_id not in SUPPORTED_DEVICES:
        raise HTTPException(status_code=404, detail="unsupported device")
    return get_device_code_store().get(device_id)


@router.post("/api/admin/device-codes/{device_id}/rotate")
async def rotate_device_code(device_id: str, session: dict[str, Any] = Depends(require_admin_csrf)):
    if device_id not in SUPPORTED_DEVICES:
        raise HTTPException(status_code=404, detail="unsupported device")
    result = get_device_code_store().get(device_id, rotate=True)
    audit_event("device_code_rotated", session, device_id=device_id, version=result["version"])
    return result


@router.post("/api/admin/devices/bind")
async def bind_device(req: DeviceBindRequest, session: dict[str, Any] = Depends(require_admin_csrf)):
    device_id = get_device_code_store().validate(req.code)
    if device_id is None:
        raise HTTPException(status_code=400, detail="invalid device code")
    cards = group_cards_by_device(get_registry().get_all_cards()).get(device_id, [])
    if not cards:
        raise HTTPException(status_code=404, detail="device card has not been published")
    added = get_space_registry().add_device(device_id)
    audit_event("device_bound", session, device_id=device_id, result="added" if added else "already_added")
    summary_keys = ("unit_id", "device_id", "name", "agent_type", "online", "description", "skills")
    return {
        "device_id": device_id,
        "result": "added" if added else "already_added",
        "cards": [{key: card[key] for key in summary_keys if key in card} for card in cards],
    }


@router.post("/api/visitor/session")
async def create_visitor_session(req: VisitorSessionRequest, request: Request, response: Response):
    check_rate_limit(request, "visitor-session")
    invite = get_space_registry().get_space()["invite"]
    valid = bool(invite["enabled"] and invite["share_id"])
    valid = valid and hmac.compare_digest(req.token, invite["share_id"])
    if not valid:
        audit_event("visitor_exchange_rejected")
        raise HTTPException(status_code=401, detail="invalid or disabled invitation")
    session = new_session("visitor", invite_version=int(invite["version"]))
    set_session_cookies(response, session)
    from cloud.api.intents import schedule_session_cleanup
    schedule_session_cleanup(session)
    audit_event("visitor_session_created", session, invite_version=invite["version"])
    return public_session(session)


@router.get("/api/me")
async def get_me(session: dict[str, Any] = Depends(require_session)):
    return public_session(session)


@router.delete("/api/session")
async def delete_session(
    response: Response,
    session: dict[str, Any] = Depends(require_csrf),
):
    # Lazy import avoids a router import cycle.
    from cloud.api.intents import end_session

    await end_session(session, reason="logout")
    clear_session_cookies(response)
    return {"ok": True}


def _public_space() -> dict[str, Any]:
    data = get_space_registry().get_space()
    data["invite"] = {
        "enabled": data["invite"]["enabled"],
        "version": data["invite"]["version"],
        "updated_at": data["invite"]["updated_at"],
    }
    return data


@router.get("/api/admin/space")
async def get_admin_space(_session: dict[str, Any] = Depends(require_admin)):
    return _public_space()


@router.patch("/api/admin/space")
async def patch_admin_space(
    req: SpacePatchRequest,
    session: dict[str, Any] = Depends(require_admin_csrf),
):
    if req.name is None and req.device_ids is None:
        raise HTTPException(status_code=400, detail="no changes supplied")
    try:
        get_space_registry().update_space(name=req.name, device_ids=req.device_ids, removals_only=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_event("space_updated", session, changed=[
        key for key, value in (("name", req.name), ("device_ids", req.device_ids)) if value is not None
    ])
    return _public_space()


@router.get("/api/admin/device-candidates")
async def get_admin_device_candidates(_session: dict[str, Any] = Depends(require_admin)):
    """Show device groups currently available for onboarding."""
    configured = set(get_space_registry().get_space()["device_ids"])
    groups = group_cards_by_device(get_registry().get_all_cards())
    labels = {
        "esp32_desk": ("桌面智能灯", "ESP32 控制器 · 灯光与传感器"),
        "go2": ("Go2 机器狗", "四足机器人与移动能力"),
    }
    return [
        {
            "device_id": device_id,
            "name": labels[device_id][0],
            "description": labels[device_id][1],
            "online": any(card.get("online", False) for card in groups.get(device_id, [])),
            "added": device_id in configured,
        }
        for device_id in ("esp32_desk", "go2")
    ]


def _admin_invite(response: Response, invite: dict[str, Any]) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {key: invite[key] for key in ("enabled", "version", "updated_at", "share_id")}


@router.get("/api/admin/invite")
async def get_admin_invite(response: Response, _session: dict[str, Any] = Depends(require_admin)):
    return _admin_invite(response, get_space_registry().get_invite())


@router.post("/api/admin/invite/enable")
async def enable_admin_invite(response: Response, session: dict[str, Any] = Depends(require_admin_csrf)):
    invite = get_space_registry().enable_invite()
    audit_event("invite_enabled", session, invite_version=invite["version"])
    return _admin_invite(response, invite)


@router.post("/api/admin/invite/disable")
async def disable_admin_invite(response: Response, session: dict[str, Any] = Depends(require_admin_csrf)):
    invite = get_space_registry().disable_invite()
    audit_event("invite_disabled", session, invite_version=invite["version"])
    return _admin_invite(response, invite)
