"""空间级人员库 API；独立于任何设备的连接状态。"""

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cloud.api.auth import require_csrf
from cloud.space.execution import ExecutionContext, require_admin_read, use_context
from cloud.space.people.provider import DuplicateFaceCandidate, FaceImageRejected, FaceProviderError
from cloud.space.people.service import space_people

router = APIRouter(prefix="/api/people", tags=["space-people"])


class PersonCreateRequest(BaseModel):
    name: str
    role: str
    image: str


def _admin(session: dict) -> None:
    if session["actor"] != "admin":
        raise HTTPException(status_code=403, detail="administrator permission required")


def _public(person) -> dict:
    return {"person_id": person.person_id, "name": person.name, "role": person.role, "face_count": person.face_count}


@router.get("")
async def list_people(context=Depends(require_admin_read)):
    return [_public(person) for person in space_people.list_people()]


@router.post("")
async def create_person(req: PersonCreateRequest, session: dict = Depends(require_csrf)):
    _admin(session)
    try:
        image = base64.b64decode(req.image, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="照片数据无效") from exc
    try:
        with use_context(ExecutionContext(session["session_id"], "admin", True)):
            person = await space_people.create(req.name, req.role, image)
    except (ValueError, DuplicateFaceCandidate, FaceImageRejected) as exc:
        raise HTTPException(status_code=409, detail=getattr(exc, "code", str(exc))) from exc
    except FaceProviderError as exc:
        raise HTTPException(status_code=503, detail=exc.code) from exc
    return _public(person)


@router.delete("/{person_id}")
async def delete_person(person_id: str, session: dict = Depends(require_csrf)):
    _admin(session)
    try:
        with use_context(ExecutionContext(session["session_id"], "admin", True)):
            await space_people.delete(person_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FaceProviderError as exc:
        raise HTTPException(status_code=503, detail=exc.code) from exc
    return {"ok": True, "person_id": person_id}
