"""Administrator personality settings for known agents."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from cloud.agent_runtime.personality import RevisionConflict
from cloud.api.auth import require_admin, require_admin_csrf

router = APIRouter(prefix="/api/agents", tags=["personality"])


class PersonalityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt
    prompt: str | None = None
    traits: dict[str, StrictInt] = Field(default_factory=dict)


def _module(agent_id: str):
    if agent_id == "cloud_orchestrator":
        from cloud.orchestrator.agentcore.personality import profile
    elif agent_id == "esp32_desk_led":
        from cloud.esp32.agentcore.personality import profile
    elif agent_id == "go2":
        from cloud.go2.agentcore.personality import profile
    else:
        raise HTTPException(status_code=404, detail="unknown agent")
    return profile


@router.get("/{agent_id}/personality")
def get_personality(agent_id: str, _session: dict = Depends(require_admin)):
    return _module(agent_id).get_profile()


@router.put("/{agent_id}/personality")
def put_personality(agent_id: str, request: PersonalityUpdate,
                    session: dict = Depends(require_admin_csrf)):
    changes = request.model_dump(exclude_unset=True)
    try:
        return _module(agent_id).update_profile(changes, session)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
