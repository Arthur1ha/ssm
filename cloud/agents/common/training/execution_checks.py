"""Mock transport checks of the original lamp execution and receipt ledger."""
from copy import deepcopy
from dataclasses import replace
import importlib
import time

import pytest

from cloud.agents.common.training.catalog import catalog_for
from cloud.agents.common.training.store import commit
from cloud.esp32.capability_chain_checks import boundary, card, UNIT
from cloud.esp32 import physical_tasks
from cloud.esp32.tool_executor import execute_physical


@pytest.fixture(autouse=True)
def isolated_training(boundary, monkeypatch, tmp_path):
    from cloud.agents.common.training import service
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path / 'training'))
    monkeypatch.setattr(service, '_send_loaded_ack', lambda *args, **kwargs: True, raising=False)
    boundary.state.on_agent_msg(UNIT, 'state', {'ism': 'OFF', 'brightness': 120},
                                received_at=time.time())


def teaching(*, teaching_id='blink', action='BLINK', src=None, defaults=None,
             cooldown=None, conditions=None, origins=None, trigger=None):
    catalog = catalog_for(card())
    edges = [e['id'] for e in catalog['transitions']
             if e['action'] == action and e['trainable'] and (src is None or e['src'] == src)]
    now = time.time()
    record = {'teaching_id': teaching_id, 'unit_id': UNIT, 'space_id': 'main',
              'name': teaching_id, 'source_text': 'Actual user text',
              'capability_hash': catalog['hash'], 'history': [],
              'created_at': now, 'updated_at': now, 'schema_version': 1,
              'revision': 1, 'loaded_revision': None, 'desired_status': 'enabled',
              'definition': {'transition_ids': edges,
                             'origins': origins or ['user_delegated', 'agent_autonomous'],
                             'conditions': conditions or [], 'cooldown_s': cooldown,
                             'defaults': defaults or {}, 'trigger': trigger}}
    return commit(record, expected_revision=0, proposal_id='proposal_' + teaching_id)


def revise(record, **fields):
    updated = deepcopy(record)
    updated.update(fields)
    return commit(updated, expected_revision=record['revision'],
                  proposal_id=f"rev_{record['teaching_id']}_{record['revision']}")


def run_context(boundary, context, *, task_id, source='lamp_chat', action='BLINK', params=None,
                device_card=None, **kwargs):
    return execute_physical(state=boundary.state, unit_id=UNIT, action=action,
                            params={'count': 2} if params is None else params,
                            context=context, dispatch=boundary.dispatch, source=source,
                            task_id=task_id, card=card() if device_card is None else device_card,
                            wait=False, **kwargs)


def test_lamp_trained_defaults_and_explicit_override(boundary):
    teaching(defaults={'count': 2})
    first = boundary.run(action='BLINK', params={}, task_id='first')
    second = boundary.run(action='BLINK', params={'count': 5}, task_id='second')
    assert first['result'] == second['result'] == 'accepted'
    assert [call['params']['count'] for call in boundary.calls] == [2, 5]
    assert first['training_refs'] == [{'teaching_id': 'blink', 'revision': 1}]
    assert first['transition_id']
    assert boundary.run('BLINK', {'count': -1}, task_id='invalid')['result'] != 'accepted'
    assert len(boundary.calls) == 2


@pytest.mark.parametrize('source,autonomous', [
    ('user_button', False), ('lamp_chat', False), ('steward', False),
    ('peer', False), ('lamp_reactive', True),
])
def test_five_trusted_ingress_contexts_share_cooldown(boundary, source, autonomous):
    teaching(src='OFF', cooldown=60)
    context = replace(boundary.context,
                      origin_kind='agent_autonomous' if autonomous else 'user_delegated')
    assert run_context(boundary, context, task_id='first', source=source)['result'] == 'accepted'
    physical_tasks.record_result('first', UNIT, {'result': 'ok'})
    blocked = run_context(boundary, context, task_id='second', source=source)
    assert blocked['result'] == 'blocked'
    assert any(r['code'] == 'cooldown' for r in blocked['reasons'])
    assert len(boundary.calls) == 1


@pytest.mark.parametrize('receipt', [None, 'accepted', 'executing', 'timeout'])
def test_pending_or_timeout_never_starts_completion_cooldown(boundary, receipt):
    teaching(src='OFF', cooldown=60)
    assert boundary.run('BLINK', {'count': 2}, task_id='first')['result'] == 'accepted'
    if receipt:
        physical_tasks.record_result('first', UNIT, {'result': receipt})
    blocked = boundary.run('BLINK', {'count': 2}, task_id='second')
    assert blocked['result'] == 'blocked'
    assert any(r['code'] == 'completion_history_unknown' for r in blocked['reasons'])
    assert len(boundary.calls) == 1
    physical_tasks.record_result('first', UNIT, {'result': 'ok'})
    blocked = boundary.run('BLINK', {'count': 2}, task_id='third')
    assert any(r['code'] == 'cooldown' for r in blocked['reasons'])


def test_replay_precedes_changed_defaults_and_paused_policy(boundary, monkeypatch):
    from cloud.agents.common.training import service
    record = teaching(defaults={'count': 2})
    assert boundary.run('BLINK', {}, task_id='repeat')['result'] == 'accepted'
    definition = deepcopy(record['definition'])
    definition['defaults']['count'] = 3
    record = revise(record, definition=definition)
    monkeypatch.setattr(service, 'load_runtime', lambda *args: pytest.fail('replay loaded policy'))
    assert boundary.run('BLINK', {}, task_id='repeat')['result'] == 'unknown'
    assert boundary.run('BLINK', {'count': 2}, task_id='repeat')['error'] == 'physical_task_id_conflict'
    record = revise(record, desired_status='removed')
    physical_tasks.record_result('repeat', UNIT, {'result': 'ok'})
    result = boundary.run('BLINK', {}, task_id='repeat')
    assert result['result'] == 'ok'
    assert result['training_refs'][0]['revision'] == 1
    assert len(boundary.calls) == 1


def test_owner_facts_never_inferred_from_admin_or_source(boundary):
    teaching(conditions=[{'field': 'requester_is_owner', 'op': 'eq', 'value': True}])
    for index, requester in enumerate((None, {'is_owner': True},
            {'is_owner': True, 'verified': True, 'expires_at': time.time() - 1},
            {'is_owner': False, 'verified': True, 'expires_at': time.time() + 60})):
        context = replace(boundary.context, requester=requester)
        assert run_context(boundary, context, task_id=f'blocked_{index}', source='owner')['result'] == 'blocked'
    assert boundary.calls == []
    context = replace(boundary.context, actor='visitor', requester={
        'is_owner': True, 'verified': True, 'expires_at': time.time() + 60})
    assert run_context(boundary, context, task_id='owner')['result'] == 'accepted'


def test_stale_or_missing_state_and_ambiguous_edge_fail_closed(boundary):
    teaching(defaults={'count': 2})
    boundary.state.on_agent_msg(UNIT, 'state', {'ism': 'OFF', 'brightness': 120})
    assert boundary.run('BLINK', {}, task_id='missing')['result'] == 'blocked'
    boundary.state.on_agent_msg(UNIT, 'state', {'ism': 'OFF', 'brightness': 120},
                                received_at=time.time() - 31)
    assert boundary.run('BLINK', {}, task_id='stale')['result'] == 'blocked'
    boundary.state.on_agent_msg(UNIT, 'state', {'ism': 'UNKNOWN'}, received_at=time.time())
    assert boundary.run('BLINK', {}, task_id='unknown')['result'] == 'blocked'
    assert boundary.calls == []


def test_fixed_state_selector_never_applies_another_target_guard(boundary):
    record = teaching(action='SET_STATE', conditions=[{
        'field': 'requester_is_owner', 'op': 'eq', 'value': True}])
    catalog = catalog_for(card())
    wanted = next(e['id'] for e in catalog['transitions']
                  if e['action'] == 'SET_STATE' and e['src'] == 'OFF' and e['dst'] == 'ON')
    definition = deepcopy(record['definition'])
    definition['transition_ids'] = [wanted]
    revise(record, definition=definition)
    assert boundary.run('SET_STATE', {'state': 'ON'}, task_id='on')['result'] == 'blocked'
    assert boundary.run('SET_STATE', {'state': 'OFF'}, task_id='off')['result'] == 'accepted'
    assert boundary.run('SET_STATE', {}, task_id='omitted')['result'] != 'accepted'
    assert len(boundary.calls) == 1


def test_runtime_load_revision_and_pause_races_recheck_current_partition(boundary, monkeypatch):
    from cloud.agents.common.training import service
    record = teaching(defaults={'count': 2})
    original_load = service.load_runtime
    def revised_before_lock(*args):
        nonlocal record
        result = original_load(*args)
        definition = deepcopy(record['definition'])
        definition['defaults']['count'] = 3
        record = revise(record, definition=definition)
        return result
    monkeypatch.setattr(service, 'load_runtime', revised_before_lock)
    assert boundary.run('BLINK', {}, task_id='revision')['result'] == 'accepted'
    assert boundary.calls[-1]['params']['count'] == 3
    def paused_before_lock(*args):
        nonlocal record
        result = original_load(*args)
        record = revise(record, desired_status='disabled')
        return result
    monkeypatch.setattr(service, 'load_runtime', paused_before_lock)
    # Pausing removes the trained finite count and restores continuous blinking.
    assert boundary.run('BLINK', {}, task_id='pause')['result'] == 'accepted'
    assert boundary.calls[-1]['params'] == {'count': 0}
    assert len(boundary.calls) == 2


def test_state_rechecked_before_authoritative_reservation(boundary):
    teaching(defaults={'count': 2})
    def changed_state(*args):
        boundary.state.on_agent_msg(UNIT, 'state', {'ism': 'ON', 'brightness': 100},
                                    received_at=time.time())
    result = boundary.run('BLINK', {}, task_id='changed', preflight=changed_state)
    assert result['error'] == 'state_changed_before_dispatch'
    assert physical_tasks.get('changed', unit_id=UNIT, session_id=boundary.context.session_id) is None
    assert boundary.calls == []


def test_cooldown_survives_revision_restart_and_has_teaching_baseline(boundary):
    # A successful untrained action before first enable is outside the baseline.
    assert boundary.run('BLINK', {'count': 2}, task_id='old')['result'] == 'accepted'
    physical_tasks.record_result('old', UNIT, {'result': 'ok'})
    record = teaching(src='OFF', cooldown=60)
    assert boundary.run('BLINK', {'count': 2}, task_id='trained')['result'] == 'accepted'
    physical_tasks.record_result('trained', UNIT, {'result': 'ok'})
    definition = deepcopy(record['definition'])
    definition['cooldown_s'] = 30
    revise(record, definition=definition)
    import cloud.esp32.tool_executor as executor
    importlib.reload(executor)
    result = boundary.run('BLINK', {'count': 2}, task_id='restarted')
    assert result['result'] == 'blocked'
    assert any(r['code'] == 'cooldown' for r in result['reasons'])
    assert len(boundary.calls) == 2


def test_cooldown_history_scoped_by_origin_teaching_and_event(boundary):
    record = teaching(src='OFF', cooldown=60)
    edge = record['definition']['transition_ids'][0]
    assert boundary.run('BLINK', {'count': 2}, task_id='manual')['result'] == 'accepted'
    physical_tasks.record_result('manual', UNIT, {'result': 'ok'})
    for scope in ({'origin_kind': 'agent_autonomous'},
                  {'origin_kind': 'user_delegated', 'trigger_ref': 'another_event'}):
        assert physical_tasks.completion_history(unit_id=UNIT, transition_id=edge,
            teaching_id='blink', since=record['created_at'], **scope) == (True, None)
    assert physical_tasks.completion_history(unit_id=UNIT, transition_id=edge,
        teaching_id='other', since=record['created_at']) == (True, None)


def test_automatic_device_feedback_is_not_trainable():
    catalog = catalog_for(card())
    automatic = [edge for edge in catalog['transitions'] if edge['trigger'] == 'BLINK_DONE']
    assert automatic and all(not edge['trainable'] for edge in automatic)
    assert all(edge['src'] != 'ERROR' and edge['dst'] != 'ERROR' for edge in catalog['transitions'])
