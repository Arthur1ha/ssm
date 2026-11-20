"""Parameter-only browser edits preserve the real teaching and execution boundary."""
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest
from pydantic import ValidationError

from cloud.agents.common.training import service, store
from cloud.agents.common.training.catalog import catalog_for
from cloud.agents.common.training.models import ParameterEdit
from cloud.esp32.capability_chain_checks import card as lamp_card


@pytest.fixture
def editing(monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path / 'training'))
    lamp = lamp_card()
    custom = {'unit_id': 'custom', 'skills': [{
        'invoke': {'action': 'SIGNAL'}, 'params_schema': {
            'type': 'object', 'properties': {
                'level': {'type': 'number', 'minimum': .1, 'maximum': 1},
                'style': {'type': 'string', 'enum': ['soft', 'sharp']},
                'mode': {'type': 'string', 'enum': ['signal']}}}}],
        'observations': [{'id': 'object.appeared', 'name': '看到',
                          'fields': {'target': 'string', 'detected': 'boolean'}}],
        'state_machine': {'regions': [{'id': 'body', 'state_field': 'body_state',
            'states': ['idle', 'active'], 'transitions': [{
                'src': 'idle', 'dst': 'active', 'trigger': 'signal', 'action': 'SIGNAL',
                'label': '发出信号', 'params': {'mode': 'signal'},
                'training_fixed_params': ['mode'], 'training_events': ['object.appeared']}]}]}}
    cards = {card['unit_id']: card for card in (lamp, custom)}
    monkeypatch.setattr(service, '_targets', lambda space: cards if space == 'main' else {})
    events = []
    monkeypatch.setattr(service, '_emit', lambda event, **details: events.append((event, details)))
    session = {'session_id': 'parameter-editor', 'actor': 'admin', 'space_id': 'main',
               'expires_at': time.time() + 300}

    def save(card, definition, name='原规则'):
        request = {'unit_id': card['unit_id'], 'operation': 'create', 'name': name,
                   'definition': definition, 'expected_revision': 0,
                   'capability_hash': catalog_for(card)['hash']}
        trusted = {**session, 'training_source': service.UserText(session['session_id'], '用户原话')}
        draft = service.propose(request, trusted)
        return service.confirm(draft['proposal_id'], 0, session)

    lamp_catalog = catalog_for(lamp)
    blink = save(lamp, {'emotion': 'happy', 'origins': ['user_delegated', 'agent_autonomous'],
        'steps': [{'transition_ids': [edge['id'] for edge in lamp_catalog['transitions']
                                    if edge['action'] == 'BLINK'], 'defaults': {'count': 5}}]})
    edge = catalog_for(custom)['transitions'][0]
    signal = save(custom, {'origins': ['agent_autonomous'],
        'trigger': {'event_id': 'object.appeared', 'where': {'target': '书本', 'detected': True}},
        'cooldown_s': 10, 'steps': [{'transition_ids': [edge['id']],
            'conditions': [{'field': 'body_state', 'op': 'eq', 'value': 'idle'}],
            'defaults': {'level': .4, 'style': 'soft'}}]})
    return SimpleNamespace(session=session, blink=blink, signal=signal, events=events,
                           cards=cards, root=tmp_path / 'training')


def edit(env, record, **patch):
    return service.update_parameters(record['teaching_id'], {
        'expected_revision': record['revision'], 'capability_hash': record['capability_hash'],
        **patch}, env.session)


def test_action_parameters_update_one_rule_all_alternatives_and_history(editing):
    old = editing.blink
    saved = edit(editing, old, steps=[{'step_index': 0, 'params': {'count': 3}}])
    assert saved['teaching_id'] == old['teaching_id'] and saved['revision'] == 2
    assert saved['definition']['steps'][0]['transition_ids'] == old['definition']['steps'][0]['transition_ids']
    assert saved['definition']['steps'][0]['defaults'] == {'count': 3}
    assert saved['history'][0]['definition']['steps'][0]['defaults'] == {'count': 5}
    assert saved['loaded_revision'] is None
    assert 'count=3' in saved['name'] and 'count=3' in saved['effect_text']
    assert service.list_proposals(editing.session, unit_id=old['unit_id'])['proposals'][-1]['operation'] == 'create'
    snapshot = service.snapshot(old['unit_id'], editing.session)
    assert len(snapshot['records']) == 1 and not snapshot['proposals']
    assert all(edge['teachings'][0]['revision'] == 2 for edge in snapshot['edge_annotations'])


def test_generic_event_and_action_parameter_edit_keeps_guards_and_identity(editing):
    old = editing.signal
    saved = edit(editing, old, event_params={'target': '水杯'}, cooldown_s=30,
                 steps=[{'step_index': 0, 'params': {'level': .7, 'style': 'sharp'}}])
    definition = saved['definition']
    assert definition['trigger'] == {'event_id': 'object.appeared', 'where': {'target': '水杯', 'detected': True}}
    assert definition['steps'][0]['defaults'] == {'level': .7, 'style': 'sharp'}
    assert definition['steps'][0]['conditions'] == old['definition']['steps'][0]['conditions']
    assert definition['origins'] == old['definition']['origins'] and definition['cooldown_s'] == 30


@pytest.mark.parametrize('patch', [
    {'steps': [{'step_index': 0, 'params': {'count': 21}}]},
    {'steps': [{'step_index': 0, 'params': {'count': True}}]},
    {'steps': [{'step_index': 0, 'params': {'r': 255}}]},
    {'steps': [{'step_index': 7, 'params': {'count': 3}}]},
    {'steps': [{'step_index': 0, 'params': {'count': 3}}, {'step_index': 0, 'params': {'count': 4}}]},
    {'event_params': {'target': '水杯'}},
    {'cooldown_s': 20},
    {'emotion': 'excited'},
])
def test_invalid_or_new_parameters_leave_saved_record_unchanged(editing, patch):
    with pytest.raises((service.TrainingError, ValidationError)):
        edit(editing, editing.blink, **patch)
    assert store.list_records(editing.blink['unit_id'], space_id='main')[0]['revision'] == 1


def test_fixed_parameter_and_event_field_type_cannot_be_changed(editing):
    with pytest.raises(service.TrainingError):
        edit(editing, editing.signal, steps=[{'step_index': 0, 'params': {'mode': 'signal'}}])
    with pytest.raises(service.TrainingError):
        edit(editing, editing.signal, event_params={'detected': 'true'})


@pytest.mark.parametrize('field', ['action', 'event_id', 'definition', 'transition_ids', 'conditions', 'source_kind'])
def test_request_contract_cannot_change_rule_structure(editing, field):
    with pytest.raises(ValidationError):
        ParameterEdit.model_validate({'expected_revision': 1, 'capability_hash': editing.blink['capability_hash'], field: 'changed'})


def test_revision_capability_and_authorization_rechecked(editing):
    old = editing.blink
    changed = edit(editing, old, emotion='relaxed')
    with pytest.raises(service.TrainingError, match='revision_conflict'):
        edit(editing, old, emotion='happy')
    with pytest.raises(service.TrainingError, match='capability_changed'):
        service.update_parameters(changed['teaching_id'], {
            'expected_revision': 2, 'capability_hash': '0' * 64, 'emotion': 'happy'}, editing.session)
    with pytest.raises(service.TrainingError, match='administrator_required'):
        service.update_parameters(changed['teaching_id'], {
            'expected_revision': 2, 'capability_hash': changed['capability_hash'], 'emotion': 'happy'},
            {**editing.session, 'actor': 'visitor'})


def test_unchanged_save_does_not_increment_revision(editing):
    result = edit(editing, editing.blink, steps=[{'step_index': 0, 'params': {'count': 5}}])
    assert result['revision'] == 1
