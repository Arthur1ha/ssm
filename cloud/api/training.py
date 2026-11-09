"""Training configuration API. It never invokes a device or a model."""
import base64
import binascii
import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request

from cloud.agents.common.training import service, store
from cloud.agents.common.training.models import (
    BrowserProposal, ConfirmRequest, EnabledRequest, LoadedRequest, ProposalRequest, TrustedSource,
)
from cloud.api.auth import require_admin, require_admin_csrf, require_session
from cloud.space.execution import ExecutionContext, require_service_context, require_authenticated_read

router = APIRouter(tags=['training'])


async def _reader(context=Depends(require_authenticated_read)):
    return asdict(context) if isinstance(context, ExecutionContext) else context


async def _proposer(request: Request):
    if request.headers.get('X-SSM-Service-Token'):
        context = await require_authenticated_read(request)
        if context.actor != 'admin' or not context.persist or context.origin_kind != 'user_delegated':
            raise HTTPException(403, {'code': 'administrator_required'})
        # The authenticated adapter derives this header from the server-owned
        # active run's latest accepted utterance. Tool arguments contain neither
        # this envelope nor user_text. Session binding prevents cross-run reuse
        # across users; the run_id is retained with the proposal for tracing.
        raw = request.headers.get('X-SSM-Training-Source', '')
        try:
            if len(raw) > 24000:
                raise ValueError('source header too large')
            source = TrustedSource.model_validate(json.loads(base64.b64decode(raw, validate=True)))
            if source.session_id != context.session_id:
                raise ValueError('source session mismatch')
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(422, {'code': 'trusted_user_text_required'}) from exc
        return {**asdict(context), 'training_source': service.UserText(
            session_id=source.session_id, text=source.text, run_id=source.run_id)}
    from cloud.api.auth import require_csrf
    session = await require_csrf(request, await require_session(request))
    return await require_admin_csrf(session)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except store.StorageError as exc:
        raise HTTPException(500, {'code': str(exc)}) from exc
    except service.TrainingError as exc:
        raise HTTPException(exc.status_code, {'code': exc.code, 'details': exc.details}) from exc
    except OSError as exc:
        raise HTTPException(500, {'code': 'training_storage_unavailable'}) from exc
    except ValueError as exc:
        raise HTTPException(422, {'code': 'invalid_training_request', 'details': str(exc)}) from exc


@router.get('/api/training')
def training_snapshot(unit_id: str, session: dict = Depends(_reader)):
    return _call(service.snapshot, unit_id, session)


@router.get('/api/devices/{unit_id}/training')
def device_training(unit_id: str, session: dict = Depends(_reader)):
    return _call(service.snapshot, unit_id, session)


@router.post('/api/training/proposals', status_code=201)
def propose_training(request: BrowserProposal | ProposalRequest, session: dict = Depends(_proposer)):
    # This is the actual authenticated user's utterance in this HTTP request.
    # Model/tool arguments cannot set source_text or inject trusted context.
    if 'training_source' in session:
        if isinstance(request, BrowserProposal):
            raise HTTPException(422, {'code': 'model_cannot_supply_user_text'})
        return _call(service.propose, request.model_dump(), session)
    if not isinstance(request, BrowserProposal):
        raise HTTPException(422, {'code': 'user_text_required'})
    context = {**session, 'training_source': service.UserText(session_id=session['session_id'], text=request.user_text)}
    return _call(service.propose, request.model_dump(exclude={'user_text'}), context)


@router.get('/api/training/proposals')
def proposal_history(unit_id: str | None = None, session: dict = Depends(require_admin)):
    return _call(service.list_proposals, session, unit_id=unit_id)


@router.get('/api/training/proposals/{proposal_id}')
def get_proposal(proposal_id: str, session: dict = Depends(require_admin)):
    return _call(service.get_proposal, proposal_id, session)


@router.post('/api/training/proposals/{proposal_id}/confirm')
def confirm_training(proposal_id: str, request: ConfirmRequest, session: dict = Depends(require_admin_csrf)):
    return _call(service.confirm, proposal_id, request.expected_revision, session,
                 capability_hash=request.capability_hash)


@router.post('/api/training/proposals/{proposal_id}/cancel')
def cancel_training(proposal_id: str, session: dict = Depends(require_admin_csrf)):
    return _call(service.cancel, proposal_id, session)


@router.put('/api/training/{teaching_id}/enabled')
def set_enabled(teaching_id: str, request: EnabledRequest, session: dict = Depends(require_admin_csrf)):
    return _call(service.set_enabled, teaching_id, request.enabled, request.expected_revision, session)


@router.post('/api/internal/training/{unit_id}/loaded')
def loaded(unit_id: str, request: LoadedRequest, context: ExecutionContext = Depends(require_service_context)):
    # The service token authenticates a trusted service, not a browser, model,
    # requester identity or a distributed executor lease. Unit/revision/hash are
    # checked again under the same partition lock used by configuration writes.
    if context.actor != 'admin' or not context.persist:
        raise HTTPException(403, {'code': 'internal_executor_required'})
    trusted = service.ExecutorContext(executor_id=request.executor_id, space_id=context.space_id)
    return _call(service.acknowledge, unit_id, request.model_dump()['revisions'], trusted)
