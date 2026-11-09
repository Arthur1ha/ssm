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
from cloud.agents.common.training.policy import (
    conflicts, validate_definition, definition_transition_ids, step_parameters, evaluate,
)
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


def _condition_text(condition, catalog):
    field, value = condition['field'], condition['value']
    if field == 'requester_is_owner' and condition['op'] == 'eq':
        return '请求者是主人' if value else '请求者不是主人'
    spec = catalog['state_fields'].get(field, {})
    values = value if condition['op'] == 'in' else [value]
    labels = spec.get('labels', {})
    rendered = '或'.join(str(labels.get(item, item)) for item in values)
    return str(spec.get('label') or field) + '为' + rendered


def _explain(record, catalog):
    edges = {edge['id']: edge for edge in catalog['transitions']}
    definition = record['definition']
    summaries = []
    for index, step in enumerate(definition['steps']):
        selected = [edges[key] for key in step['transition_ids'] if key in edges]
        label = '、'.join(dict.fromkeys(
            f"{edge.get('source_label', edge['src'])} → {edge.get('destination_label', edge['dst'])}"
            for edge in selected)) or '目标转换能力已变化'
        goal_text = None
        if step.get('goal_state') and selected:
            goal_text = '已经' + selected[0].get('destination_label', step['goal_state']) + '时跳过'
        summaries.append({'step_index': index, 'label': label,
                          'transition_ids': list(step['transition_ids']),
                          'conditions': deepcopy(step['conditions']),
                          'conditionText': '，且'.join(_condition_text(item, catalog) for item in step['conditions']),
                          'defaults': deepcopy(step['defaults']),
                          'goal_state': step.get('goal_state'), 'goalText': goal_text})
    return {'transitionLabel': '，然后'.join(step['label'] for step in summaries),
            'summaryText': record['name'],
            'changeText': ('删除这条规则：' + record['name'] if record['desired_status'] == 'removed'
                           else record.get('effect_text') or record['name']),
            'scopeText': '仅此事件触发的行为' if definition['trigger'] else '、'.join(
                {'user_delegated': '用户委托请求', 'agent_autonomous': '自主请求'}[origin]
                for origin in definition['origins']),
            'transition_ids': definition_transition_ids(definition), 'stepSummaries': summaries}


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
            record = {'schema_version': 2, 'teaching_id': previous['record']['teaching_id'] if previous else 'trn_' + uuid4().hex,
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
            for edge_id in definition_transition_ids(record['definition']):
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


def resolve_training_step(record, catalog, step_index, current_state, overrides=None):
    """Resolve one confirmed step against the current authoritative snapshot."""
    _check_catalog(record, catalog)
    if len(record['definition']['steps']) > 1 and overrides:
        raise TrainingError('ambiguous_overrides: specify a separate teaching revision for a multi-step parameter change')
    prepared = step_parameters(record, catalog, step_index, overrides)
    edges = [edge for edge in catalog['transitions'] if edge['id'] in prepared['transition_ids']]
    goal = prepared['goal_state']
    field = edges[0]['state_field']
    parent = edges[0].get('parent')
    if parent and current_state.get(parent['state_field']) != parent['state']:
        raise TrainingError('step_parent_state_unavailable')
    connected = current_state.get('connected', current_state.get('_connected', current_state.get('online'))) is True
    if field == 'body_state':
        fresh = current_state.get('body_telemetry_fresh') is True
    elif field in {'life_state', 'activity_state'}:
        fresh = connected
    else:
        received = current_state.get('_received_at')
        fresh = type(received) in (int, float) and 0 <= time.time() - received <= 30
    if goal is not None and current_state.get(field) == goal:
        if not connected or not fresh:
            raise TrainingError('state_unknown')
        return {**prepared, 'transition_id': None, 'skip': True}
    matching = [edge for edge in edges if current_state.get(edge['state_field']) == edge['src']]
    if len(matching) != 1:
        raise TrainingError('source_state_unavailable' if not matching else 'transition_ambiguous')
    prepared = step_parameters(record, catalog, step_index, overrides, transition_id=matching[0]['id'])
    return {**prepared, 'transition_id': matching[0]['id'], 'skip': False}


def _selected_application(records, card, catalog, current, action, params, context):
    reference = context.training_ref or context.trigger_ref
    if reference is None:
        if context.training_revision is not None or context.trigger_revision is not None:
            raise TrainingError('training_selection_invalid')
        return None, None
    selected = next((row for row in records if row['teaching_id'] == reference), None)
    if (selected is None or selected['desired_status'] != 'enabled'
            or selected['unit_id'] != card['unit_id'] or selected['space_id'] != context.space_id
            or context.origin_kind not in selected['definition']['origins']
            or (context.training_ref and context.trigger_ref and context.training_ref != context.trigger_ref)):
        raise TrainingError('training_selection_invalid')
    revision = context.training_revision if context.training_ref else context.trigger_revision
    if type(revision) is not int or selected['revision'] != revision:
        raise TrainingError('training_revision_changed', 409)
    _check_catalog(selected, catalog)
    index = (getattr(context, 'training_step_index', None) if context.training_ref
             else getattr(context, 'trigger_step_index', None))
    if type(index) is not int or not 0 <= index < len(selected['definition']['steps']):
        raise TrainingError('training_step_invalid')
    if selected['definition']['trigger']:
        if (context.trigger_ref != reference or context.origin_kind != 'agent_autonomous'
                or context.trigger_revision != selected['revision']
                or getattr(context, 'trigger_step_index', None) != index
                or (context.trigger_observation_id is not None and
                    context.trigger_observation_id != selected['definition']['trigger']['event_id'])):
            raise TrainingError('trigger_authorization_invalid')
    elif context.trigger_ref:
        raise TrainingError('trigger_authorization_invalid')
    overrides = context.training_overrides or {}
    if len(selected['definition']['steps']) > 1 and overrides:
        raise TrainingError('ambiguous_overrides')
    choices = [edge for edge in catalog['transitions']
               if edge['id'] in selected['definition']['steps'][index]['transition_ids']
               and current.get(edge['state_field']) == edge['src']]
    if len(choices) != 1:
        raise TrainingError('source_state_unavailable' if not choices else 'transition_ambiguous')
    prepared = step_parameters(selected, catalog, index, overrides, transition_id=choices[0]['id'])
    if prepared['action'] != action or prepared['params'] != params:
        raise TrainingError('training_parameters_changed')
    return selected, index


def evaluate_execution(card, current, action, params, context, records, completion_history, *, state_fresh=True):
    """Every device boundary enforces current matching step guards and defaults."""
    result = {'allowed': True, 'params': dict(params), 'reasons': [], 'training_refs': [], 'transition_id': None}
    if action in {'STOP', 'StopMove'}:
        return result
    catalog = catalog_for(card)
    records = list(records)
    try:
        selected, selected_index = _selected_application(records, card, catalog, current, action, params, context)
    except (TrainingError, ValueError) as exc:
        return {**result, 'allowed': False, 'reasons': [{'code': getattr(exc, 'code', str(exc))}]}
    action_edges = [edge for edge in catalog['transitions'] if edge['action'] == action
                    and all(params.get(key) == value for key, value in edge['selector_params'].items())]
    current_edges = [edge for edge in action_edges if current.get(edge['state_field']) == edge['src']]
    if len(current_edges) == 1 and not current_edges[0]['trainable'] and selected is None:
        return result
    candidates = [edge for edge in current_edges if edge['trainable']]
    applicable = []
    action_ids = {edge['id'] for edge in action_edges}
    for record in records:
        definition = record['definition']
        if record['desired_status'] != 'enabled' or context.origin_kind not in definition['origins']:
            continue
        is_selected = selected is not None and selected['teaching_id'] == record['teaching_id']
        if definition['trigger'] and context.trigger_ref != record['teaching_id']:
            continue
        if definition.get('situation') and not is_selected:
            continue
        if action_ids.intersection(definition_transition_ids(definition)):
            applicable.append(record)
    if len(candidates) != 1:
        if applicable or selected is not None:
            result['reasons'].append({'code': 'transition_unknown' if not candidates else 'transition_ambiguous'})
        result['allowed'] = not result['reasons']
        return result
    edge = candidates[0]
    result['transition_id'] = edge['id']
    if selected is not None:
        ids = selected['definition']['steps'][selected_index]['transition_ids']
        if edge['id'] not in ids:
            result['reasons'].append({'code': 'training_transition_mismatch'})
        if context.trigger_ref and edge['id'] != context.trigger_transition_id:
            result['reasons'].append({'code': 'trigger_authorization_invalid'})
    applicable = [record for record in applicable if edge['id'] in definition_transition_ids(record['definition'])]
    if applicable and not state_fresh:
        result['reasons'].append({'code': 'state_unknown'})
    defaults, selected_defaults = {}, {}
    for record in applicable:
        try:
            _check_catalog(record, catalog)
        except TrainingError as exc:
            result['reasons'].append({'code': exc.code})
            continue
        is_selected = selected is not None and selected['teaching_id'] == record['teaching_id']
        known, last = completion_history(record, edge['id']) if record['definition']['cooldown_s'] is not None else (True, None)
        checked = evaluate([dict(record, loaded_revision=record['revision'])],
            {'transition_id': edge['id'], 'origin': context.origin_kind, 'params': {},
             'trigger_ref': context.trigger_ref, 'training_ref': context.training_ref,
             'step_index': selected_index if is_selected else None,
             'trigger_situation_confirmed': context.trigger_situation_confirmed},
            {'state': current, 'state_fresh': state_fresh, 'requester': context.requester,
             'completion_history_known': known, 'last_completed_at': {edge['id']: last}}, now=time.time())
        result['reasons'].extend(checked['reasons'])
        result['training_refs'].extend(checked['teaching_refs'])
        if is_selected:
            selected_defaults.update(checked['params'])
        else:
            for key, value in checked['params'].items():
                if key in defaults and defaults[key] != value:
                    result['reasons'].append({'code': 'default_conflict', 'field': key})
                defaults[key] = value
    result['params'] = {**defaults, **selected_defaults, **params}
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
    """Snapshot eligibility describes the first step, never a dispatch grant."""
    from cloud.agents.common.effects import FileEffectLedger
    definition = record['definition']
    origin = 'user_delegated' if 'user_delegated' in definition['origins'] else 'agent_autonomous'
    if card.get('agent_type') == 'robot':
        from cloud.go2.connection import go2
        current = go2.state_snapshot()
        connected = current.get('connected') is True
        ledger = FileEffectLedger()
        def history(edge_id):
            return ledger.completion_history(unit_id=card['unit_id'], transition_id=edge_id,
                teaching_id=record['teaching_id'], since=teaching_baseline(record),
                origin_kind=origin, trigger_ref=record['teaching_id'] if definition['trigger'] else None)
    else:
        import sys
        state = getattr(sys.modules.get('cloud.api.main'), '_esp32_state', None)
        entry = state.actuator_snapshot().get(card['unit_id'], {}) if state else {}
        current = {**(entry.get('state') or {}), 'connected': card.get('online') is True}
        current.setdefault('_received_at', entry.get('_received_at'))
        connected = current['connected']
        from cloud.esp32.physical_tasks import completion_history
        def history(edge_id):
            return completion_history(unit_id=card['unit_id'], transition_id=edge_id,
                teaching_id=record['teaching_id'], since=teaching_baseline(record),
                origin_kind=origin, trigger_ref=record['teaching_id'] if definition['trigger'] else None)
    if not connected:
        return [{'code': 'device_offline'}]
    try:
        prepared = resolve_training_step(record, catalog, 0, current)
    except (TrainingError, ValueError) as exc:
        return [{'code': getattr(exc, 'code', str(exc))}]
    if prepared['skip']:
        return []
    edge = next(edge for edge in catalog['transitions'] if edge['id'] == prepared['transition_id'])
    field = edge['state_field']
    fresh = (connected if field in {'activity_state', 'life_state'} else
             current.get('body_telemetry_fresh') is True if field == 'body_state' else
             type(current.get('_received_at')) in (int, float) and 0 <= time.time()-current['_received_at'] <= 30)
    if not fresh:
        return [{'code': 'state_unknown'}]
    known, last = history(edge['id']) if definition['cooldown_s'] is not None else (True, None)
    checked = evaluate([dict(record, loaded_revision=record['revision'])],
        {'transition_id': edge['id'], 'origin': origin, 'params': prepared['params'],
         'trigger_ref': record['teaching_id'] if definition['trigger'] else None,
         'training_ref': record['teaching_id'], 'step_index': 0},
        {'state': current, 'state_fresh': fresh, 'requester': None,
         'completion_history_known': known, 'last_completed_at': {edge['id']: last}}, now=time.time())
    reasons = checked['reasons']
    if definition['trigger']:
        reasons.append({'code': 'fresh_trigger_required'})
    elif origin == 'agent_autonomous':
        reasons.append({'code': 'autonomy_context_required'})
    return reasons


def clear_training(*, space_id='main', unit_ids=None):
    """API-owned maintenance used for the authorized clean schema cutover."""
    _IDENTIFIER.validate_python(space_id)
    units = store.partition_units(space_id=space_id) if unit_ids is None else list(unit_ids)
    results = []
    for unit_id in units:
        partition = store.clear_partition(unit_id, space_id=space_id)
        result = {'space_id': space_id, 'unit_id': unit_id, 'cursor': partition['cursor']}
        _emit('training.reset', **result)
        results.append(result)
    return results
