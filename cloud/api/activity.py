"""Authenticated space activity lifecycle and mode reconciliation."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from cloud.api.auth import require_csrf, require_session
from cloud.cards.registry import get_registry
from cloud.space.activity import get_activity_store
from cloud.space.execution import ExecutionContext, require_service_context
from cloud.space.registry import DEFAULT_SPACE_ID, card_device_id, get_space_registry

router = APIRouter(tags=["activity"])
_transition_lock = asyncio.Lock()


class StartActivityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=4000)
    source_event_id: str = Field(..., pattern=r"^evt_[A-Fa-f0-9]{32}$")
    user_revision: int = Field(..., ge=1)


class BrowserStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=4000)


def _selected_mode(card: dict[str, Any]) -> str | None:
    """Read available autonomy options from the live card, then apply unit policy."""
    axes = card.get("modes") or []
    autonomy = next((axis for axis in axes if axis.get("id") == "autonomy"), None)
    if autonomy is None:
        return None
    offered = {option.get("value") for option in autonomy.get("options") or []}
    preferred = "free_explore" if card.get("unit_id") == "go2" else "reactive"
    return preferred if preferred in offered else next(
        (mode for mode in offered if mode and mode != "manual"), None)


def _activity_cards() -> list[dict[str, Any]]:
    configured = set(get_space_registry().get_space()["device_ids"])
    return [card for card in get_registry().get_all_cards().values()
            if card_device_id(card) in configured and _selected_mode(card)]


async def _switch_mode(unit_id: str, mode: str, activity_id: str) -> dict[str, Any]:
    if unit_id == "go2":
        from cloud.go2.router import set_activity_mode, go2_autonomy_get

        result = await set_activity_mode(mode, activity_id)
        readback = await go2_autonomy_get()
    elif unit_id == os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led"):
        from cloud.esp32.router import set_activity_mode
        from cloud.esp32.agent import get_agent

        result = set_activity_mode(mode, activity_id)
        agent = get_agent()
        readback = {"mode": agent.get_autonomy_mode() if agent else "unknown"}
    else:
        raise RuntimeError("activity_mode_adapter_unavailable")
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError(str((result or {}).get("reason") or "activity_mode_rejected"))
    if readback.get("mode") != mode:
        raise RuntimeError("activity_mode_not_confirmed")
    return result


async def _end_locked(reason: str) -> dict[str, Any] | None:
    store = get_activity_store()
    record = store.begin_end(reason)
    if record is None:
        return None
    activity_id = record["activity_id"]
    devices = record["devices"]
    for unit_id, state in devices.items():
        if state.get("owner") != activity_id:
            continue
        try:
            result = await _switch_mode(unit_id, "manual", activity_id)
            state["status"] = "left"
            state["owner"] = ""
            if "physical_stop" in result:
                state["physical_stop"] = result["physical_stop"]
        except Exception as exc:
            state["status"] = "unknown"
            state["error"] = type(exc).__name__
    return store.finish_end(activity_id, devices)


async def end_current_activity(reason: str = "user_intervention") -> dict[str, Any] | None:
    async with _transition_lock:
        return await _end_locked(reason)


async def reconcile_current_activity() -> None:
    """After API restart, inspect modes without repeating old physical actions."""
    async with _transition_lock:
        store = get_activity_store()
        record = store.current()
        if not record:
            return
        if record["status"] in {"starting", "ending"}:
            await _end_locked("recovery")
            return
        activity_id = record["activity_id"]
        for unit_id, state in record.get("devices", {}).items():
            if state.get("status") != "joined":
                continue
            if unit_id == "go2":
                from cloud.go2.router import get_activity_mode_owner, go2_autonomy_get
                owner = get_activity_mode_owner()
                mode = (await go2_autonomy_get()).get("mode")
            elif unit_id == os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led"):
                from cloud.esp32.router import get_activity_mode_owner
                from cloud.esp32.agent import get_agent
                owner = get_activity_mode_owner()
                agent = get_agent()
                mode = agent.get_autonomy_mode() if agent else "unknown"
            else:
                owner, mode = None, "unknown"
            if owner != activity_id or mode != state.get("mode"):
                store.set_device(activity_id, unit_id, mode=state.get("mode") or "",
                                 status="unknown", error="mode_unconfirmed_after_restart")
        store.activate(activity_id)


async def user_intervention(source_event_id: str = "") -> int:
    """Call after the API authenticated a new user write, before executing it."""
    revision = get_activity_store().note_user_intervention(source_event_id)
    await end_current_activity("user_intervention")
    return revision


async def start_activity(text: str, source_event_id: str, user_revision: int,
                         source_session_id: str = "") -> dict[str, Any]:
    store = get_activity_store()
    async with _transition_lock:
        if not store.matches_user_request(source_event_id, user_revision):
            raise HTTPException(status_code=409, detail="activity request was superseded by a newer user request")
        await _end_locked("new_activity")
        try:
            record = store.begin(text, source_event_id, user_revision, source_session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        activity_id = record["activity_id"]
        try:
            cards = _activity_cards()
        except Exception:
            await _end_locked("start_failed")
            raise
        for card in cards:
            if not store.matches_user_request(source_event_id, user_revision):
                await _end_locked("superseded_user_request")
                raise HTTPException(status_code=409, detail="activity request was superseded")
            unit_id = card["unit_id"]
            mode = _selected_mode(card)
            if not card.get("online"):
                store.set_device(activity_id, unit_id, mode=mode or "", status="offline", error="device_offline")
                continue
            try:
                await _switch_mode(unit_id, mode or "", activity_id)
                store.set_device(activity_id, unit_id, mode=mode or "", status="joined", owner=activity_id)
            except Exception as exc:
                store.set_device(activity_id, unit_id, mode=mode or "", status="unavailable",
                                 error=type(exc).__name__)
        if not store.matches_user_request(source_event_id, user_revision):
            await _end_locked("superseded_user_request")
            raise HTTPException(status_code=409, detail="activity request was superseded")
        return store.activate(activity_id) or store.get(activity_id) or record


@router.get("/api/activity/current")
async def get_current_activity(session: dict[str, Any] = Depends(require_session)):
    store = get_activity_store()
    return {"activity": store.current(session["space_id"]), "latest_activity_id": store.latest_id()}


@router.get("/api/activity/{activity_id}/events")
async def get_activity_events(activity_id: str, session: dict[str, Any] = Depends(require_session)):
    store = get_activity_store()
    record = store.get(activity_id)
    if record is None or record.get("space_id") != session["space_id"]:
        raise HTTPException(status_code=404, detail="activity not found")
    return {"activity_id": activity_id, "events": store.events(activity_id)}


@router.post("/api/activity/end")
async def end_activity(session: dict[str, Any] = Depends(require_csrf)):
    await user_intervention("evt_" + uuid.uuid4().hex)
    record = get_activity_store().get(get_activity_store().latest_id() or "")
    return {"ok": True, "activity": record if record and record.get("status") == "ended" else None}


@router.post("/api/activity/start")
async def browser_start_activity(req: BrowserStartRequest, session: dict[str, Any] = Depends(require_csrf)):
    source_event_id = "evt_" + uuid.uuid4().hex
    revision = await user_intervention(source_event_id)
    activity = await start_activity(req.text, source_event_id, revision, session["session_id"])
    return {"ok": True, "activity": activity}


@router.post("/api/internal/activity/start", include_in_schema=False)
async def internal_start_activity(req: StartActivityRequest,
                                  context: ExecutionContext = Depends(require_service_context)):
    if (context.origin_kind != "user_delegated" or context.expires_at <= time.time()
            or context.space_id != DEFAULT_SPACE_ID):
        raise HTTPException(status_code=403, detail="live delegated user request required")
    activity = await start_activity(req.text, req.source_event_id, req.user_revision, context.session_id)
    return {"ok": True, "activity": activity}


@router.get("/api/internal/activity/current", include_in_schema=False)
async def internal_current_activity(context: ExecutionContext = Depends(require_service_context)):
    return {"activity": get_activity_store().current(context.space_id)}
