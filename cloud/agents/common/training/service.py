"""Proposal/confirmation service owned by the API; no device execution here.

UserText and ExecutorContext are constructed by trusted API adapters, never from
model arguments. Loading is informational: later executors must use the store's
partition lock and revalidate each reservation against the current definition.
"""
from copy import deepcopy
from dataclasses import dataclass
import time
from typing import Callable
from uuid import uuid4

from pydantic import TypeAdapter
from cloud.agents.common.training.catalog import catalog_for
from cloud.agents.common.training.models import (
    Identifier, LoadedRequest, Proposal, ProposalRequest, Record,
)
from cloud.agents.common.training.policy import conflicts, validate_definition
from cloud.agents.common.training import store

_IDENTIFIER = TypeAdapter(Identifier)
_BOOT_ID = 'boot_' + uuid4().hex
_event_sink: Callable | None = None


class TrainingError(ValueError):
    def __init__(self, code: str, status_code: int = 422, *, details=None):
        super().__init__(code)
        self.code, self.status_code, self.details = code, status_code, details


@dataclass(frozen=True)
class UserText:
    session_id: str
    text: str
    run_id: str | None = None


@dataclass(frozen=True)
class ExecutorContext:
    executor_id: str
    space_id: str


def configure_events(sink: Callable) -> None:
    global _event_sink
    _event_sink = sink


def _emit(event, **context):
    if _event_sink:
        _event_sink(event, **context)


def _now():
    return time.time()


def _session(session, *, admin=False):
    if not isinstance(session, dict) or session.get('actor') not in ('admin', 'visitor'):
        raise TrainingError('authentication_required', 401)
    if session.get('expires_at', 0) <= _now():
        raise TrainingError('session_expired', 401)
    _IDENTIFIER.validate_python(session.get('session_id'))
    _IDENTIFIER.validate_python(session.get('space_id'))
    if admin and session['actor'] != 'admin':
        raise TrainingError('administrator_required', 403)


def _targets(space_id):
    from cloud.cards.registry import get_registry
    from cloud.space.registry import get_space_cards, get_space_registry
    registry = get_space_registry()
    if registry.get_space()['id'] != space_id:
        return {}
    cards = get_registry().get_all_cards()
    return {uid: card for uid, card in get_space_cards(cards, registry).items()
            if card.get('agent_type') != 'sensor'}


def _target(unit_id, space_id):
    _IDENTIFIER.validate_python(unit_id)
    card = _targets(space_id).get(unit_id)
    if card is None:
        raise TrainingError('device_not_in_space', 404)
    return card


def _definition_reasons(record):
    # A persistent rule describes future requests. Availability of an identity
    # observation now is not a property of that definition. The executor checks
    # fresh, server-owned requester facts on every matching action instead.
    return []


def _check_catalog(record, catalog):
    if record['capability_hash'] != catalog['hash']:
        raise TrainingError('capability_changed', 409)
    try:
        validate_definition(record['definition'], catalog)
    except ValueError as exc:
        raise TrainingError('invalid_definition', details=str(exc)) from exc


def _check_conflicts(record, data):
    if record['desired_status'] != 'enabled':
        return
    found = conflicts(record['definition'], [r for key, r in data['records'].items()
                                           if key != record['teaching_id']])
    if found:
        raise TrainingError('training_conflict', 409, details=found)


def _commit(data, record, expected_revision, proposal_id):
    try:
        return store.commit_locked(data, record, expected_revision=expected_revision, proposal_id=proposal_id)
    except store.StorageError:
        raise
    except ValueError as exc:
        code = str(exc)
        raise TrainingError(code, 409 if code == 'revision_conflict' else 422) from exc


def _explain(record, catalog):
    edges = {edge['id']: edge for edge in catalog['transitions']}
    selected = [edges[key] for key in record['definition']['transition_ids'] if key in edges]
    definition = record['definition']
    trigger = definition['trigger']
    # The existing role Graph supplies these presentation strings alongside the
    # structured definition. They never participate in execution or permission
    # checks. Old records retain their own user-facing name without exposing JSON.
    summary = record['name']
    return {'transitionLabel': '、'.join(f"{edge.get('source_label', edge['src'])} → {edge.get('destination_label', edge['dst'])}" for edge in selected) or '目标转换能力已变化',
            'summaryText': summary,
            'changeText': ('删除这条规则：' + summary if record['desired_status'] == 'removed'
                           else record.get('effect_text') or summary),
            'scopeText': '仅此事件触发的请求' if trigger else '、'.join(
                {'user_delegated': '用户委托请求', 'agent_autonomous': '自主请求'}[origin]
                for origin in definition['origins']),
            'transition_ids': list(definition['transition_ids'])}


def _preview(proposal, data, catalog):
    record = deepcopy(proposal['record'])
    reasons = _definition_reasons(record)
    current = data['records'].get(record['teaching_id'])
    confirmed = proposal['proposal_id'] in data['confirmed_proposals']
    cancelled = proposal.get('cancelled_at') is not None
    superseded = bool(proposal.get('superseded_by'))
    expired = proposal['expires_at'] <= _now() and not confirmed
    if not confirmed:
        for inactive, code in ((cancelled, 'proposal_cancelled'), (superseded, 'proposal_superseded'), (expired, 'proposal_expired')):
            if inactive:
                reasons.append({'code': code})
        if record['capability_hash'] != catalog['hash']:
            reasons.append({'code': 'capability_changed'})
        if (current['revision'] if current else 0) != proposal['request']['expected_revision']:
            reasons.append({'code': 'revision_conflict'})
    before = deepcopy(current)
    # Preview retains the exact base version even after this proposal commits.
    if current and current['revision'] > proposal['request']['expected_revision']:
        base = proposal['request']['expected_revision']
        before = deepcopy(current['history'][base - 1]) if base else None
    previous = data['proposals'].get(proposal['request'].get('supersedes_proposal_id'))
    if previous:
        before = deepcopy(previous['record'])
    return {'proposal_id': proposal['proposal_id'], 'operation': proposal['request']['operation'],
            'unit_id': record['unit_id'], 'teaching_id': record['teaching_id'],
            'expected_revision': proposal['request']['expected_revision'],
            'capability_hash': record['capability_hash'], 'expires_at': proposal['expires_at'],
            'before': before, 'after': record, 'confirmation_allowed': not reasons,
            'beforeSummaryText': before['name'] if before else None,
            'afterSummaryText': record['name'],
            'source_text': record['source_text'], 'source_run_id': proposal.get('source_run_id'),
            'created_at': proposal['created_at'],
            'reasons': reasons, 'confirmed': confirmed, 'cancelled': cancelled,
            'superseded': superseded, 'superseded_by': proposal.get('superseded_by'), 'expired': expired,
            **_explain(record, catalog)}


def propose(request: dict, session: dict) -> dict:
    _session(session, admin=True)
    req = ProposalRequest.model_validate(request).model_dump()
    source = session.get('training_source')
    if not isinstance(source, UserText) or source.session_id != session['session_id'] or not isinstance(source.text, str) or not source.text.strip() or len(source.text) > 4000:
        raise TrainingError('trusted_user_text_required')
    card = _target(req['unit_id'], session['space_id'])
    catalog = catalog_for(card)
    if req['capability_hash'] != catalog['hash']:
        raise TrainingError('capability_changed', 409)
    now = _now()
    with store.partition_lock(req['unit_id'], space_id=session['space_id'], write=True) as data:
        previous = None
        if req['supersedes_proposal_id']:
            previous = _proposal(data, req['supersedes_proposal_id'], session)
            if req['supersedes_proposal_id'] in data['confirmed_proposals']:
                raise TrainingError('proposal_already_confirmed', 409)
            old = previous['request']
            if (req['teaching_id'], req['expected_revision']) != (old['teaching_id'], old['expected_revision']):
                raise TrainingError('proposal_base_mismatch', 409)
        current = data['records'].get(req['teaching_id'])
        if req['operation'] != 'create':
            if current is None:
                raise TrainingError('teaching_not_found', 404)
            if current['revision'] != req['expected_revision']:
                raise TrainingError('revision_conflict', 409)
            if current['desired_status'] == 'removed':
                raise TrainingError('teaching_removed', 409)
            record = deepcopy(current)
            record.update(name=req['name'] or current['name'], source_text=source.text,
                          capability_hash=catalog['hash'], loaded_revision=None)
            if req['operation'] == 'remove':
                record['desired_status'] = 'removed'
            else:
                record['definition'] = req['definition']
                record['effect_text'] = req['effect_text']
        else:
            record = {'schema_version': 1, 'teaching_id': previous['record']['teaching_id'] if previous else 'trn_' + uuid4().hex,
                      'unit_id': req['unit_id'], 'space_id': session['space_id'], 'name': req['name'],
                      'effect_text': req['effect_text'],
                      'source_text': source.text, 'revision': 1, 'definition': req['definition'],
                      'desired_status': 'enabled', 'capability_hash': catalog['hash'],
                      'loaded_revision': None, 'history': [], 'created_at': now, 'updated_at': now}
        if req['operation'] != 'remove':
            _check_catalog(record, catalog)
        _check_conflicts(record, data)
        proposal = Proposal.model_validate({'proposal_id': 'prp_' + uuid4().hex,
            'session_id': session['session_id'], 'source_run_id': source.run_id, 'request': req, 'record': record,
            'created_at': now, 'expires_at': now + 1800}).model_dump()
        # Expired drafts cannot execute, but retain them for a meaningful 410.
        # Bounded draft capacity refuses growth rather than silently evicting.
        pending = [p for key, p in data['proposals'].items()
                   if key not in data['confirmed_proposals'] and p['expires_at'] > now
                   and not p.get('cancelled_at') and not p.get('superseded_by')]
        if len(pending) >= 500:
            raise TrainingError('pending_proposal_limit')
        # Keep expired IDs as tombstones via the proposal itself, bounded by
        # a total cap. A maintenance/retention policy is intentionally deferred.
        if len(data['proposals']) >= 51000:
            raise TrainingError('proposal_storage_limit')
        data['proposals'][proposal['proposal_id']] = proposal
        if previous:
            previous['superseded_by'] = proposal['proposal_id']
        preview = _preview(proposal, data, catalog)
    _emit('training.proposed', space_id=session['space_id'], unit_id=req['unit_id'],
          proposal_id=proposal['proposal_id'], session_id=session['session_id'])
    return preview


def _find_proposal(proposal_id, session):
    _IDENTIFIER.validate_python(proposal_id)
    for unit_id in _targets(session['space_id']):
        with store.partition_lock(unit_id, space_id=session['space_id']) as data:
            p = data['proposals'].get(proposal_id)
            if p and p['session_id'] == session['session_id']:
                return unit_id
    raise TrainingError('proposal_not_found', 404)


def _proposal(data, proposal_id, session, *, active=True):
    p = data['proposals'].get(proposal_id)
    if p is None or p['session_id'] != session['session_id']:
        raise TrainingError('proposal_not_found', 404)
    if active and proposal_id not in data['confirmed_proposals']:
        if p.get('cancelled_at') is not None:
            raise TrainingError('proposal_cancelled', 409)
        if p.get('superseded_by'):
            raise TrainingError('proposal_superseded', 409)
        if p['expires_at'] <= _now():
            raise TrainingError('proposal_expired', 410)
    return p


def get_proposal(proposal_id: str, session: dict) -> dict:
    _session(session, admin=True)
    unit_id = _find_proposal(proposal_id, session)
    with store.partition_lock(unit_id, space_id=session['space_id']) as data:
        return _preview(_proposal(data, proposal_id, session, active=False), data, catalog_for(_target(unit_id, session['space_id'])))


def list_proposals(session: dict, *, unit_id: str | None = None) -> dict:
    """Restore conversation cards, including drafts that can no longer commit."""
    _session(session, admin=True)
    cards = {unit_id: _target(unit_id, session['space_id'])} if unit_id else _targets(session['space_id'])
    previews = []
    for uid, card in cards.items():
        catalog = catalog_for(card)
        with store.partition_lock(uid, space_id=session['space_id']) as data:
            previews.extend(_preview(p, data, catalog) for p in data['proposals'].values()
                            if p['session_id'] == session['session_id'])
    return {'proposals': sorted(previews, key=lambda p: (p['created_at'], p['proposal_id']))}


def cancel(proposal_id: str, session: dict) -> dict:
    _session(session, admin=True)
    unit_id = _find_proposal(proposal_id, session)
    with store.partition_lock(unit_id, space_id=session['space_id'], write=True) as data:
        proposal = _proposal(data, proposal_id, session, active=False)
        if proposal_id in data['confirmed_proposals']:
            raise TrainingError('proposal_already_confirmed', 409)
        if proposal.get('cancelled_at') is None:
            proposal['cancelled_at'] = _now()
        preview = _preview(proposal, data, catalog_for(_target(unit_id, session['space_id'])))
    _emit('training.updated', space_id=session['space_id'], unit_id=unit_id,
          proposal_id=proposal_id, session_id=session['session_id'])
    return preview


def confirm(proposal_id: str, expected_revision: int, session: dict, *, capability_hash: str | None = None) -> dict:
    _session(session, admin=True)
    unit_id = _find_proposal(proposal_id, session)
    with store.partition_lock(unit_id, space_id=session['space_id'], write=True) as data:
        proposal = _proposal(data, proposal_id, session)
        if type(expected_revision) is not int or expected_revision != proposal['request']['expected_revision']:
            raise TrainingError('revision_conflict', 409)
        record = proposal['record']
        if capability_hash is not None and capability_hash != record['capability_hash']:
            raise TrainingError('capability_changed', 409)
        # Authorization above still applies to retries. Return the original
        # committed result before rechecking changing capabilities or revision.
        if proposal_id in data['confirmed_proposals']:
            return deepcopy(data['confirmed_proposals'][proposal_id])
        catalog = catalog_for(_target(unit_id, session['space_id']))
        if record['capability_hash'] != catalog['hash']:
            raise TrainingError('capability_changed', 409)
        if proposal['request']['operation'] != 'remove':
            _check_catalog(record, catalog)
        reasons = _definition_reasons(record)
        if reasons:
            raise TrainingError(reasons[0]['code'], details=reasons)
        _check_conflicts(record, data)
        result = _commit(data, record, expected_revision, proposal_id)
        cursor = data['cursor']
    _emit('training.updated', space_id=session['space_id'], unit_id=unit_id,
          teaching_id=result['teaching_id'], revision=result['revision'], cursor=cursor)
    return result


def _find_record(teaching_id, session):
    _IDENTIFIER.validate_python(teaching_id)
    for unit_id in _targets(session['space_id']):
        with store.partition_lock(unit_id, space_id=session['space_id']) as data:
            if teaching_id in data['records']:
                return unit_id
    raise TrainingError('teaching_not_found', 404)


def set_enabled(teaching_id: str, enabled: bool, expected_revision: int, session: dict) -> dict:
    _session(session, admin=True)
    if type(enabled) is not bool:
        raise TrainingError('invalid enabled')
    unit_id = _find_record(teaching_id, session)
    with store.partition_lock(unit_id, space_id=session['space_id'], write=True) as data:
        current = data['records'][teaching_id]
        if current['desired_status'] == 'removed':
            raise TrainingError('teaching_removed', 409)
        record = deepcopy(current) | {'desired_status': 'enabled' if enabled else 'disabled'}
        card = _target(unit_id, session['space_id'])
        if enabled:
            _check_catalog(record, catalog_for(card))
            reasons = _definition_reasons(record)
            if reasons:
                raise TrainingError(reasons[0]['code'], details=reasons)
            _check_conflicts(record, data)
        result = _commit(data, record, expected_revision, 'toggle_' + uuid4().hex)
        cursor = data['cursor']
    _emit('training.updated', space_id=session['space_id'], unit_id=unit_id,
          teaching_id=teaching_id, revision=result['revision'], cursor=cursor)
    return result


def snapshot(unit_id: str, viewer: dict) -> dict:
    _session(viewer)
    card = _target(unit_id, viewer['space_id'])
    catalog = catalog_for(card)
    with store.partition_lock(unit_id, space_id=viewer['space_id']) as data:
        projected, annotations = [], {}
        for current in data['records'].values():
            record = deepcopy(current)
            loaded = record['loaded_revision'] == record['revision'] and data['loaded_epoch'].get(record['teaching_id']) == _BOOT_ID
            if not loaded:
                record['loaded_revision'] = None
            reasons = _definition_reasons(record)
            if record['desired_status'] != 'enabled':
                reasons.append({'code': record['desired_status']})
            if record['capability_hash'] != catalog['hash']:
                reasons.append({'code': 'capability_changed'})
            if not loaded:
                reasons.append({'code': 'awaiting_load'})
            if not reasons:
                reasons.extend(_snapshot_execution_reasons(card, record, viewer, catalog))
            record.update(saved=True, load_status='loaded' if loaded else 'awaiting_load',
                          executable=not reasons, execution_reasons=reasons, **_explain(record, catalog))
            for edge_id in record['definition']['transition_ids']:
                annotations.setdefault(edge_id, []).append({
                    'teaching_id': record['teaching_id'], 'revision': record['revision'],
                    'desired_status': record['desired_status'], 'load_status': record['load_status'],
                    'changeText': record['changeText'], 'scopeText': record['scopeText'],
                    'execution_reasons': deepcopy(reasons)})
            if viewer['actor'] != 'admin':
                record.pop('source_text')
                for old in record['history']:
                    old.pop('source_text')
            projected.append(record)
        return {'unit_id': unit_id, 'space_id': viewer['space_id'], 'catalog': catalog,
                'cursor': data['cursor'], 'records': projected,
                'proposals': [_preview(p, data, catalog) for p in data['proposals'].values()
                              if viewer['actor'] == 'admin' and p['session_id'] == viewer['session_id']
                              and p['proposal_id'] not in data['confirmed_proposals']
                              and p.get('cancelled_at') is None and not p.get('superseded_by')
                              and p['expires_at'] > _now()],
                'edge_annotations': [{'transition_id': key, 'teachings': value} for key, value in annotations.items()]}


def acknowledge(unit_id: str, revisions: dict, service_context: ExecutorContext) -> dict:
    if not isinstance(service_context, ExecutorContext):
        raise TrainingError('internal_service_required', 403)
    _IDENTIFIER.validate_python(service_context.space_id)
    body = LoadedRequest.model_validate({'executor_id': service_context.executor_id, 'revisions': revisions}).model_dump()
    with store.partition_lock(unit_id, space_id=service_context.space_id, write=True) as data:
        card = _target(unit_id, service_context.space_id)
        expected_executor = 'go2' if card.get('agent_type') == 'robot' else 'lamp' if card.get('agent_type') == 'actuator' else None
        if body['executor_id'] != expected_executor:
            raise TrainingError('executor_target_mismatch', 403)
        catalog = catalog_for(card)
        for key, version in body['revisions'].items():
            record = data['records'].get(key)
            if record is None or version['revision'] != record['revision']:
                raise TrainingError('stale_loaded_revision', 409)
            if version['capability_hash'] != record['capability_hash'] or version['capability_hash'] != catalog['hash']:
                raise TrainingError('capability_changed', 409)
            if record['desired_status'] == 'enabled':
                _check_catalog(record, catalog)
            record['loaded_revision'] = record['revision']
            data['loaded_epoch'][key] = _BOOT_ID
        data['cursor'] += 1
        cursor = data['cursor']
    _emit('training.loaded', space_id=service_context.space_id, unit_id=unit_id, cursor=cursor)
    return {'unit_id': unit_id, 'revisions': body['revisions'], 'cursor': cursor}


def teaching_baseline(record):
    """The first enable survives pause, edits, process restarts and revisions."""
    return next((version['updated_at'] for version in record.get('history', []) + [record]
                 if version['desired_status'] == 'enabled'), record['created_at'])


def evaluate_execution(card, current, action, params, context, records, completion_history, *, state_fresh=True):
    """Pure guard/default projection; callers hold their dispatch/partition locks.

    Loaded ACKs only describe loading. Current confirmed definitions govern the
    boundary even if an informational ACK was lost or the API restarted.
    """
    from cloud.agents.common.training.policy import evaluate
    result = {'allowed': True, 'params': dict(params), 'reasons': [], 'training_refs': [], 'transition_id': None}
    if action in {'STOP', 'StopMove'}:
        return result
    catalog = catalog_for(card)
    action_edges = [edge for edge in catalog['transitions'] if edge['action'] == action]
    resolved = [edge for edge in action_edges if current.get(edge['state_field']) == edge['src']
                and all(params.get(key) == value for key, value in edge.get('selector_params', {}).items())]
    if len(resolved) == 1 and not resolved[0]['trainable']:
        return result
    action_edges = [edge for edge in action_edges if edge['trainable']]
    action_ids = {edge['id'] for edge in action_edges}
    applicable = [record for record in records if record['desired_status'] == 'enabled'
                  and context.origin_kind in record['definition']['origins']
                  and (not record['definition']['trigger'] or context.trigger_ref == record['teaching_id'])
                  and action_ids.intersection(record['definition']['transition_ids'])]
    candidates = [edge for edge in action_edges if current.get(edge['state_field']) == edge['src']
                  and all(params.get(key) == value for key, value in edge.get('selector_params', {}).items())]
    if context.trigger_ref:
        triggered = next((record for record in applicable if record['teaching_id'] == context.trigger_ref
                          and record['definition']['trigger']), None)
        if (triggered is None or context.origin_kind != 'agent_autonomous'
                or context.trigger_revision != triggered['revision']):
            result['reasons'].append({'code': 'trigger_authorization_invalid'})
    if len(candidates) != 1:
        if applicable or context.trigger_ref:
            result['reasons'].append({'code': 'transition_unknown' if not candidates else 'transition_ambiguous'})
        result['allowed'] = not result['reasons']
        return result
    edge = candidates[0]
    if context.trigger_ref and triggered is not None:
        bound = {**edge.get('selector_params', {}), **triggered['definition']['trigger']['params']}
        if (edge['id'] != context.trigger_transition_id or edge['id'] not in triggered['definition']['transition_ids']
                or params != bound):
            result['reasons'].append({'code': 'trigger_authorization_invalid'})
        params = {**bound, **params}
    result['transition_id'] = edge['id']
    applicable = [record for record in applicable if edge['id'] in record['definition']['transition_ids']]
    if applicable and not state_fresh:
        result['reasons'].append({'code': 'state_unknown'})
    defaults = {}
    for record in applicable:
        try:
            _check_catalog(record, catalog)
        except TrainingError as exc:
            result['reasons'].append({'code': exc.code})
            continue
        known, last = completion_history(record, edge['id']) if record['definition']['cooldown_s'] is not None else (True, None)
        checked = evaluate([dict(record, loaded_revision=record['revision'])],
                           {'transition_id': edge['id'], 'origin': context.origin_kind,
                            'params': params, 'trigger_ref': context.trigger_ref},
                           {'state': current, 'state_fresh': state_fresh, 'requester': context.requester,
                            'completion_history_known': known, 'last_completed_at': {edge['id']: last}}, now=time.time())
        result['reasons'].extend(checked['reasons'])
        result['training_refs'].extend(checked['teaching_refs'])
        for key, value in record['definition']['defaults'].items():
            if key in defaults and defaults[key] != value:
                result['reasons'].append({'code': 'default_conflict', 'field': key})
            defaults[key] = value
    result['params'] = {**defaults, **params}
    result['allowed'] = not result['reasons']
    return result


def _send_loaded_ack(unit_id, revisions, context, executor_id):
    """Existing internal API is the sole writer, also from orchestrator workers."""
    import os
    import requests
    token = os.getenv('SSM_INTERNAL_SERVICE_TOKEN', '')
    if not token:
        return False
    response = requests.post(
        os.getenv('GO2_API_BASE', 'http://127.0.0.1:8082') + '/api/internal/training/' + unit_id + '/loaded',
        json={'executor_id': executor_id, 'revisions': revisions},
        headers={'X-SSM-Service-Token': token, 'X-SSM-Session-Id': 'training-' + executor_id,
                 'X-SSM-Actor': 'admin', 'X-SSM-Persist': 'true', 'X-SSM-Space-Id': context.space_id,
                 'X-SSM-Origin-Kind': 'user_delegated'}, timeout=2, proxies={'http': None, 'https': None})
    response.raise_for_status()
    return True


def load_runtime(card, context):
    """Validate a load and acknowledge through API, outside all dispatch locks."""
    catalog = catalog_for(card)
    with store.partition_lock(card['unit_id'], space_id=context.space_id) as data:
        revisions = {}
        for record in data['records'].values():
            if record['desired_status'] == 'enabled':
                try:
                    _check_catalog(record, catalog)
                except TrainingError:
                    continue
            revisions[record['teaching_id']] = {'revision': record['revision'], 'capability_hash': record['capability_hash']}
    if revisions:
        try:
            return _send_loaded_ack(card['unit_id'], revisions, context,
                                    'go2' if card.get('agent_type') == 'robot' else 'lamp')
        except Exception:
            # An unavailable ACK affects display only, never weakens guards.
            return False
    return True



def _snapshot_execution_reasons(card, record, viewer, catalog):
    """Current eligibility only; loaded configuration is never a dispatch grant."""
    from cloud.agents.common.effects import FileEffectLedger
    from cloud.agents.common.training.policy import evaluate
    definition = record['definition']
    origin = 'user_delegated' if 'user_delegated' in definition['origins'] else 'agent_autonomous'
    if card.get('agent_type') == 'robot':
        from cloud.go2.connection import go2
        current = go2.state_snapshot()
        if not current.get('connected'):
            return [{'code': 'device_offline'}]
        fresh = current.get('body_telemetry_fresh') is True
        if not fresh:
            current = {**current, 'body_state': None, 'fsm_state': None}
        ledger = FileEffectLedger()
        def history(edge_id):
            return ledger.completion_history(unit_id=card['unit_id'], transition_id=edge_id,
                teaching_id=record['teaching_id'], since=teaching_baseline(record),
                origin_kind=origin, trigger_ref=record['teaching_id'] if definition['trigger'] else None)
    else:
        import sys
        state = getattr(sys.modules.get('cloud.api.main'), '_esp32_state', None)
        entry = state.actuator_snapshot().get(card['unit_id'], {}) if state else {}
        current = entry.get('state') or {}
        received = current.get('_received_at') or entry.get('_received_at')
        fresh = isinstance(received, (int, float)) and 0 <= time.time() - received <= 30
        if not card.get('online'):
            return [{'code': 'device_offline'}]
        from cloud.esp32.physical_tasks import completion_history
        def history(edge_id):
            return completion_history(unit_id=card['unit_id'], transition_id=edge_id,
                teaching_id=record['teaching_id'], since=teaching_baseline(record),
                origin_kind=origin, trigger_ref=record['teaching_id'] if definition['trigger'] else None)
    edges = [edge for edge in catalog['transitions'] if edge['id'] in definition['transition_ids']
             and edge['src'] == current.get(edge['state_field'])]
    if not edges:
        return [{'code': 'source_state_unavailable'}]
    edge = edges[0]
    if edge['state_field'] in {'activity_state', 'life_state'}:
        # These are local runtime facts, independent of body telemetry age.
        fresh = bool(current.get('connected'))
    if not fresh:
        return [{'code': 'state_unknown'}]
    reasons = []
    if card.get('agent_type') == 'robot':
        if edge['action'] == 'LIFE':
            base = current.get('life_blocked_reasons', {}).get(edge['selector_params'].get('action'))
        else:
            base = current.get('blocked_actions', {}).get(edge['action'])
        if base:
            reasons.append({'code': 'device_precondition', 'detail': base})
    known, last = history(edge['id']) if definition['cooldown_s'] is not None else (True, None)
    checked = evaluate([dict(record, loaded_revision=record['revision'])],
        {'transition_id': edge['id'], 'origin': origin, 'params': edge.get('selector_params', {}),
         'trigger_ref': record['teaching_id'] if definition['trigger'] else None},
        {'state': current, 'state_fresh': fresh, 'requester': None,
         'completion_history_known': known, 'last_completed_at': {edge['id']: last}}, now=time.time())
    reasons.extend(checked['reasons'])
    if definition['trigger']:
        reasons.append({'code': 'fresh_trigger_required'})
    elif origin == 'agent_autonomous':
        reasons.append({'code': 'autonomy_context_required'})
    if set(edge['params_schema'].get('required', [])) - checked['params'].keys():
        reasons.append({'code': 'request_parameters_required'})
    return reasons
