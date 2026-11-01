"""Owner-confirmed teaching memory; records are not executable rules."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from cloud.agents.common import teachings
from cloud.agents.common.teaching_bindings import validate as validate_binding
from cloud.api.auth import require_admin, require_admin_csrf
from cloud.api.policy import audit_event
from cloud.cards.registry import get_registry
from cloud.space.registry import get_space_cards, get_space_registry

router = APIRouter(prefix="/api/teachings", tags=["teachings"])


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_id: str
    name: str = Field(min_length=1, max_length=120)
    trigger: str = Field(min_length=1, max_length=500)
    behavior: str = Field(min_length=1, max_length=500)
    source_text: str = Field(min_length=1, max_length=1000)
    confirmation_id: str | None = Field(default=None, min_length=1, max_length=120)


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    trigger: str | None = Field(default=None, min_length=1, max_length=500)
    behavior: str | None = Field(default=None, min_length=1, max_length=500)


class BindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_id: str
    where: dict
    skill_id: str
    params: dict
    enabled: bool = False


def _targets(space_id: str) -> dict[str, dict]:
    if space_id != get_space_registry().get_space()["id"]:
        return {}
    all_cards = get_registry().get_all_cards()
    cards = get_space_cards(all_cards, get_space_registry())
    # A cloud agent may be resident in the space without a physical device adoption.
    cards.update({uid: card for uid, card in all_cards.items()
                  if card.get("space_id") == space_id})
    return {uid: card for uid, card in cards.items()
            if card.get("agent_type") != "sensor" and
            (card.get("services") or {}).get("teachings_endpoint") == "/api/teachings"}


def _target(unit_id: str, session: dict) -> None:
    if unit_id not in _targets(session["space_id"]):
        raise HTTPException(404, "teachable agent is not in this space")


def _all(session: dict) -> list[dict]:
    try:
        targets = _targets(session["space_id"])
        return [_project(item, targets[uid]) for uid in targets
                for item in teachings.list_for(uid, space_id=session["space_id"])]
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "teaching memory unreadable") from exc


def _project(item: dict, card: dict) -> dict:
    result = dict(item)
    if result.get("execution_status") in {"ready", "enabled"}:
        try:
            validate_binding(result.get("binding"), card)
        except (ValueError, KeyError, TypeError):
            result["execution_status"] = "capability_unavailable"
    return result


@router.get("")
async def list_teachings(unit_id: str | None = None, session: dict = Depends(require_admin)):
    if unit_id is not None:
        _target(unit_id, session)
        try:
            return [_project(item, _targets(session["space_id"])[unit_id])
                    for item in teachings.list_for(unit_id, space_id=session["space_id"])]
        except (OSError, ValueError) as exc:
            raise HTTPException(500, "teaching memory unreadable") from exc
    return _all(session)


@router.post("", status_code=201)
async def create_teaching(req: Draft, session: dict = Depends(require_admin_csrf)):
    _target(req.unit_id, session)
    if len(_all(session)) >= 2000:
        raise HTTPException(422, "space teaching limit reached")
    try:
        item = teachings.remember(req.unit_id, req.name, req.trigger, req.behavior,
                                  req.source_text, space_id=session["space_id"],
                                  confirmation_id=req.confirmation_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit_event("teaching_created", session, teaching_id=item["teaching_id"], unit_id=req.unit_id)
    return item


@router.patch("/{teaching_id}")
async def update_teaching(teaching_id: str, req: Revision,
                          session: dict = Depends(require_admin_csrf)):
    existing = next((item for item in _all(session) if item["teaching_id"] == teaching_id), None)
    if existing is None:
        raise HTTPException(404, "teaching not found")
    changes = req.model_dump(exclude_unset=True)
    if not changes or any(value is None for value in changes.values()):
        raise HTTPException(422, "revision fields required")
    try:
        item = teachings.revise(existing["unit_id"], teaching_id, changes,
                                space_id=session["space_id"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit_event("teaching_revised", session, teaching_id=teaching_id, unit_id=existing["unit_id"])
    return item


@router.put("/{teaching_id}/binding")
async def bind_teaching(teaching_id: str, req: BindingRequest,
                        session: dict = Depends(require_admin_csrf)):
    existing = next((item for item in _all(session) if item["teaching_id"] == teaching_id), None)
    if existing is None:
        raise HTTPException(404, "teaching not found")
    card = _targets(session["space_id"])[existing["unit_id"]]
    try:
        binding = validate_binding(req.model_dump(exclude={"enabled"}), card)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    item = teachings.set_binding(existing["unit_id"], teaching_id, binding,
                                 enabled=req.enabled, space_id=session["space_id"])
    audit_event("teaching_binding_updated", session, teaching_id=teaching_id,
                unit_id=existing["unit_id"], enabled=req.enabled)
    return item


@router.delete("/{teaching_id}/binding")
async def unbind_teaching(teaching_id: str, session: dict = Depends(require_admin_csrf)):
    existing = next((item for item in _all(session) if item["teaching_id"] == teaching_id), None)
    if existing is None:
        raise HTTPException(404, "teaching not found")
    item = teachings.set_binding(existing["unit_id"], teaching_id, None,
                                 space_id=session["space_id"])
    audit_event("teaching_binding_removed", session, teaching_id=teaching_id,
                unit_id=existing["unit_id"])
    return item


@router.delete("/{teaching_id}")
async def delete_teaching(teaching_id: str, session: dict = Depends(require_admin_csrf)):
    existing = next((item for item in _all(session) if item["teaching_id"] == teaching_id), None)
    if existing is None:
        raise HTTPException(404, "teaching not found")
    teachings.forget(existing["unit_id"], teaching_id, space_id=session["space_id"])
    audit_event("teaching_deleted", session, teaching_id=teaching_id, unit_id=existing["unit_id"])
    return {"deleted": teaching_id}
