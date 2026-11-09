"""Semantic selection and exact saved parameters through the existing boundary."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
import time

import pytest

from cloud.agents.common.training import service, store
from cloud.agents.common.training.catalog import catalog_for
from cloud.agents.common.training.check_fixtures import lamp_sound_fixture
from cloud.agents.common.training.models import Definition
from cloud.agents.common.training.policy import (
    conflicts, evaluate, training_event_candidates, training_event_intent, validate_definition,
)
from cloud.agents.common.training.tools import prepare_training_application
from cloud.esp32.capability_chain_checks import UNIT, boundary, card
from cloud.esp32.tool_executor import execute_physical
from cloud.space.execution import ExecutionContext


READING = {'r': 255, 'g': 236, 'b': 210, 'brightness': 200}
RESTING = {'r': 255, 'g': 100, 'b': 35, 'brightness': 50}


@pytest.fixture
def semantic(boundary, monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path / 'training'))
    monkeypatch.setattr(service, '_send_loaded_ack', lambda *_a, **_k: True)
    monkeypatch.setattr(service, '_targets', lambda _space: {UNIT: card()})
    boundary.state.on_agent_msg(UNIT, 'state', {'ism': 'OFF', 'brightness': 120},
                                received_at=time.time())
    catalog = catalog_for(card())
    session = {'session_id': boundary.context.session_id, 'actor': 'admin',
               'space_id': 'main', 'expires_at': time.time() + 60}

    def save(name, params, *, situation=None, conditions=None, action='SET_COLOR'):
        definition = {'transition_ids': [edge['id'] for edge in catalog['transitions']
                                        if edge['action'] == action and edge['trainable']],
                      'origins': ['user_delegated', 'agent_autonomous'],
                      'conditions': conditions or [], 'cooldown_s': None,
                      'defaults': params, 'trigger': None, 'situation': situation}
        session['training_source'] = service.UserText(session['session_id'], name)
        preview = service.propose({'operation': 'create', 'unit_id': UNIT,
            'name': name, 'definition': definition, 'expected_revision': 0,
            'capability_hash': catalog['hash']}, session)
        return service.confirm(preview['proposal_id'], 0, session)

    async def api(path, payload, context, *, method='POST'):
        assert method == 'GET' and payload is None
        assert path == '/api/training?unit_id=' + UNIT
        return {'catalog': catalog, 'records': store.list_records(UNIT, space_id='main')}

    boundary.save = save
    boundary.api = api
    boundary.catalog = catalog
    return boundary


async def application(semantic, record, overrides=None):
    return await prepare_training_application(
        {'unit_id': UNIT, 'teaching_id': record['teaching_id'],
         'revision': record['revision'], 'overrides': overrides or {}},
        semantic.context, semantic.api)


def dispatch(semantic, prepared, task_id, **changes):
    context = replace(semantic.context, **{
        key: prepared[key] for key in ('training_ref', 'training_revision', 'training_overrides')})
    return execute_physical(state=semantic.state, unit_id=UNIT, action=prepared['action'],
        params=prepared['params'], context=context, dispatch=semantic.dispatch,
        task_id=task_id, card=card(), wait=False, **changes)


@pytest.mark.parametrize('situation', ['阅读', '收工以后想在窗边听音乐',
                                      '练习小提琴时，谱架旁这盏灯保持现在这样'])
def test_open_situation_is_not_a_state_or_sensor_enum(semantic, situation):
    record = semantic.save('保持这档', READING, situation=situation)
    assert record['definition']['situation'] == situation
    assert validate_definition(record['definition'], semantic.catalog)['situation'] == situation


def test_legacy_definition_keeps_global_meaning(semantic):
    record = semantic.save('以后调色默认这档', READING)
    old = deepcopy(record['definition'])
    old.pop('situation')
    assert Definition.model_validate(old).situation is None
    assert validate_definition(old, semantic.catalog) == old


@pytest.mark.asyncio
@pytest.mark.parametrize('source_state', ['OFF', 'ON', 'BLINKING'])
async def test_two_situations_coexist_and_only_selected_parameters_dispatch(semantic, source_state):
    reading = semantic.save('看书用这档', READING, situation='阅读')
    semantic.save('休息用柔和光', RESTING, situation='休息')
    semantic.state.on_agent_msg(UNIT, 'state', {'ism': source_state, 'brightness': 120},
                                received_at=time.time())
    prepared = await application(semantic, reading)
    assert prepared['params'] == READING
    result = dispatch(semantic, prepared, 'reading-' + source_state)
    assert result['result'] == 'accepted'
    assert semantic.calls[-1]['params'] == READING
    assert result['training_refs'] == [{'teaching_id': reading['teaching_id'], 'revision': 1}]


@pytest.mark.asyncio
async def test_cross_session_reads_saved_values_and_partial_override_does_not_rewrite(semantic):
    reading = semantic.save('阅读配色', READING, situation='阅读')
    later = replace(semantic.context, session_id='another-session', actor='visitor')
    prepared = await prepare_training_application(
        {'unit_id': UNIT, 'teaching_id': reading['teaching_id'], 'revision': 1,
         'overrides': {'brightness': 90}}, asdict(later), semantic.api)
    assert prepared['params'] == {**READING, 'brightness': 90}
    assert prepared['training_overrides'] == {'brightness': 90}
    assert dispatch(semantic, prepared, 'dim-once')['result'] == 'accepted'
    saved = store.list_records(UNIT, space_id='main')[0]
    assert saved['definition']['defaults'] == READING
    assert saved['revision'] == 1


def test_unselected_situation_does_not_block_or_color_other_requests(semantic):
    semantic.save('阅读仅主人', READING, situation='阅读', conditions=[
        {'field': 'requester_is_owner', 'op': 'eq', 'value': True}])
    other = {'r': 1, 'g': 2, 'b': 3}
    result = semantic.run('SET_COLOR', other, task_id='ordinary')
    assert result['result'] == 'accepted' and result['training_refs'] == []
    assert semantic.calls[-1]['params'] == {**other, 'brightness': 120}


@pytest.mark.asyncio
async def test_selected_situation_cannot_bypass_global_guard(semantic):
    reading = semantic.save('阅读配色', READING, situation='阅读')
    semantic.save('调色仅主人', {}, conditions=[
        {'field': 'requester_is_owner', 'op': 'eq', 'value': True}])
    result = dispatch(semantic, await application(semantic, reading), 'guarded')
    assert result['result'] == 'blocked'
    assert result['reasons'] == [{'code': 'condition_unknown', 'field': 'requester_is_owner'}]
    assert semantic.calls == []


@pytest.mark.asyncio
async def test_global_defaults_do_not_conflict_with_selected_configuration(semantic):
    semantic.save('普通调色默认亮度', {'brightness': 130})
    reading = semantic.save('阅读配色', READING, situation='阅读')
    result = dispatch(semantic, await application(semantic, reading), 'selected-over-default')
    assert result['result'] == 'accepted'
    assert semantic.calls[-1]['params'] == READING


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revision', 'disabled', 'removed'])
async def test_dispatch_rechecks_saved_revision_and_removal_after_prepare(semantic, change):
    reading = semantic.save('阅读配色', READING, situation='阅读')
    prepared = await application(semantic, reading)
    newer = deepcopy(reading)
    if change == 'revision':
        newer['definition']['defaults']['brightness'] = 80
    else:
        newer['desired_status'] = change
    store.commit(newer, expected_revision=1, proposal_id='changed-after-prepare')
    result = dispatch(semantic, prepared, 'stale')
    assert result['result'] == 'blocked'
    expected = 'training_revision_changed' if change == 'revision' else 'training_selection_invalid'
    assert result['reasons'] == [{'code': expected}]
    assert semantic.calls == []


@pytest.mark.asyncio
async def test_dispatch_rechecks_saved_parameter_values_and_preserves_ledger_provenance(semantic, tmp_path):
    reading = semantic.save('阅读配色', READING, situation='阅读')
    prepared = await application(semantic, reading, {'brightness': 80})
    changed = deepcopy(prepared)
    changed['params']['r'] = 1
    result = dispatch(semantic, changed, 'changed-params')
    assert result['result'] == 'blocked'
    assert result['reasons'] == [{'code': 'training_parameters_changed'}]
    assert dispatch(semantic, prepared, 'exact-config')['result'] == 'accepted'
    ledger = json.loads((tmp_path / 'exact-config.json').read_text())
    assert ledger['params'] == {**READING, 'brightness': 80}
    assert ledger['training']['training_ref'] == reading['teaching_id']
    assert ledger['training']['training_revision'] == 1
    assert ledger['training']['training_overrides'] == {'brightness': 80}


@pytest.mark.asyncio
@pytest.mark.parametrize('overrides', [{'state': 'OFF'}, {'r': -1}, {'brightness': True}])
async def test_application_rejects_invalid_parameters(semantic, overrides):
    reading = semantic.save('阅读配色', READING, situation='阅读')
    with pytest.raises(ValueError):
        await application(semantic, reading, overrides)


def test_only_same_situation_defaults_compete(semantic):
    reading = semantic.save('阅读配色', READING, situation='阅读')
    different = deepcopy(reading['definition'])
    different['defaults']['r'] = 100
    assert conflicts(different, [reading])[0]['code'] == 'default_conflict'
    different['situation'] = '演奏前热身'
    assert conflicts(different, [reading]) == []


def test_pure_policy_uses_only_selected_situation_and_keeps_global_guard(semantic):
    reading = semantic.save('阅读配色', READING, situation='阅读')
    resting = semantic.save('休息配色', RESTING, situation='休息')
    rows = [dict(row, loaded_revision=row['revision']) for row in [reading, resting]]
    request = {'transition_id': reading['definition']['transition_ids'][0],
               'origin': 'user_delegated', 'params': {}, 'training_ref': reading['teaching_id']}
    result = evaluate(rows, request, {'state': {}, 'state_fresh': True}, now=100)
    assert result['allowed'] and result['params'] == READING
    assert result['teaching_refs'] == [{'teaching_id': reading['teaching_id'], 'revision': 1}]


@pytest.mark.parametrize('confirmed', [False, True])
def test_event_candidate_does_not_claim_semantic_situation_is_observed(confirmed):
    fixture = lamp_sound_fixture()
    record = fixture['records'][0]
    record['definition']['situation'] = '我正在阅读，声音打断我时'
    candidate = training_event_intent(fixture['event'], fixture['records'],
        fixture['catalog'], fixture['facts'], now=100)
    assert candidate['situation'] == record['definition']['situation']
    assert candidate['source_text'] == record['source_text']
    assert candidate['name'] == record['name']
    device_card = card()
    device_card['unit_id'] = record['unit_id']
    context = ExecutionContext('event', 'visitor', False, origin_kind='agent_autonomous',
        trigger_ref=record['teaching_id'], trigger_revision=record['revision'],
        trigger_transition_id=candidate['transition_id'],
        trigger_situation_confirmed=confirmed)
    checked = service.evaluate_execution(device_card, {'ism': 'OFF'}, 'BLINK',
        candidate['params'], context, fixture['records'], lambda *_a: (True, None))
    assert checked['allowed'] is confirmed
    assert checked['training_refs'] == [{'teaching_id': record['teaching_id'], 'revision': 1}]
    assert checked['reasons'] == ([] if confirmed else [{'code': 'situation_not_confirmed'}])


def test_same_event_can_offer_multiple_semantic_situations_to_the_graph():
    fixture = lamp_sound_fixture()
    reading = fixture['records'][0]
    reading['definition']['situation'] = '阅读时'
    resting = deepcopy(reading)
    resting.update(teaching_id='resting', name='休息时提醒')
    resting['definition']['situation'] = '休息时'
    resting['definition']['trigger']['params']['count'] = 1
    records = [reading, resting]
    assert conflicts(resting['definition'], [reading]) == []
    candidates = training_event_candidates(fixture['event'], records,
        fixture['catalog'], fixture['facts'], now=100)
    assert [row['teaching_id'] for row in candidates] == [reading['teaching_id'], 'resting']
    assert [row['situation'] for row in candidates] == ['阅读时', '休息时']
    assert [row['params']['count'] for row in candidates] == [2, 1]
    assert all('trigger_situation_confirmed' not in row for row in candidates)
    # Older adapters must not silently choose the first of multiple candidates.
    assert training_event_intent(fixture['event'], records,
        fixture['catalog'], fixture['facts'], now=100) is None
    assert training_event_intent(fixture['event'], [reading],
        fixture['catalog'], fixture['facts'], now=100) == candidates[0]
    assert training_event_candidates(fixture['event'], records,
        fixture['catalog'], fixture['facts'], now=106) == []


@pytest.mark.parametrize('situation', [None, '阅读时'])
def test_same_scope_event_conflicts_remain_rejected(situation):
    fixture = lamp_sound_fixture()
    first = fixture['records'][0]
    if situation is not None:
        first['definition']['situation'] = situation
    second = deepcopy(first)
    second['teaching_id'] = 'competing-event'
    second['definition']['trigger']['params']['count'] = 1
    assert conflicts(second['definition'], [first]) == [
        {'code': 'event_overlap', 'teaching_id': first['teaching_id']}]
