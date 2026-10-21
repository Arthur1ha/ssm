"""Authenticated device and Agent Card endpoints for the current space."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from cloud.api.auth import require_session
from cloud.cards.registry import get_registry
from cloud.space.registry import get_space_cards, get_space_registry

router = APIRouter(prefix="/api/devices", tags=["devices"])


def _card_response(card: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "unit_id", "device_id", "parent_id", "name", "description", "agent_type",
        "online", "transport", "state", "state_machine", "skills", "modes", "telemetry",
        "widgets", "suggestions", "tags",
    )
    return {key: card.get(key) for key in keys if key in card}


def _fixed_cards() -> dict[str, dict[str, Any]]:
    return get_space_cards(get_registry().get_all_cards(), get_space_registry())


@router.get("")
async def list_devices(request: Request, _session: dict[str, Any] = Depends(require_session)):
    """Return configured devices; offline configured devices remain visible."""
    legacy = {"scope", "space_id"}.intersection(request.query_params.keys())
    if legacy:
        raise HTTPException(status_code=400, detail="scope and space_id are no longer supported")
    return [_card_response(card) for card in _fixed_cards().values()]


@router.get("/{unit_id}")
async def get_device(unit_id: str, _session: dict[str, Any] = Depends(require_session)):
    card = _fixed_cards().get(unit_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"unit '{unit_id}' is not in the current space")
    return _card_response(card)


@router.get("/{unit_id}/agent")
async def device_agent_card(unit_id: str, _session: dict[str, Any] = Depends(require_session)):
    card = _fixed_cards().get(unit_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"unit '{unit_id}' is not in the current space")
    return card
