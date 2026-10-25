"""Authenticated device and Agent Card endpoints for the current space."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from cloud.api.auth import require_session
from cloud.cards.registry import get_registry
from cloud.space.registry import get_space_cards, get_space_registry

router = APIRouter(prefix="/api/devices", tags=["devices"])


def _readable_json(payload: Any) -> Response:
    """Indent card data for direct viewing without changing the JSON contract."""
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        media_type="application/json",
    )


_CARD_GROUPS = (
    ("card_schema_version", "version", "unit_id", "device_id", "parent_id", "name", "description", "agent_type", "tags"),
    ("online", "transport", "services"),
    ("skills",),
    ("state", "state_machine", "modes", "telemetry", "widgets", "suggestions"),
)


def _readable_card(card: dict[str, Any]) -> Response:
    """Group top-level Card fields while preserving a parseable JSON object."""
    seen = set()
    sections = []
    for keys in (*_CARD_GROUPS, tuple(key for key in card if key not in {k for group in _CARD_GROUPS for k in group})):
        fields = []
        for key in keys:
            if key not in card or key in seen:
                continue
            seen.add(key)
            value = json.dumps(card[key], ensure_ascii=False, indent=2)
            value = value.replace("\n", "\n  ")
            fields.append(f'  {json.dumps(key, ensure_ascii=False)}: {value}')
        if fields:
            sections.append(",\n".join(fields))
    return Response(content="{\n" + ",\n\n".join(sections) + "\n}\n", media_type="application/json")


def _card_response(card: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "card_schema_version", "version", "unit_id", "device_id", "parent_id", "name", "description", "agent_type",
        "online", "transport", "services", "state", "state_machine", "skills", "modes", "telemetry",
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
    cards = [_card_response(card) for card in _fixed_cards().values()]
    return _readable_json(cards)


@router.get("/{unit_id}")
async def get_device(unit_id: str, _session: dict[str, Any] = Depends(require_session)):
    card = _fixed_cards().get(unit_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"unit '{unit_id}' is not in the current space")
    return _readable_json(_card_response(card))


@router.get("/{unit_id}/agent")
async def device_agent_card(unit_id: str, _session: dict[str, Any] = Depends(require_session)):
    card = _fixed_cards().get(unit_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"unit '{unit_id}' is not in the current space")
    return _readable_card(card)
