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
                           cards=cards, save=save, root=tmp_path / 'training')


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


def test_explicitly_clear_existing_interval_preserves_steps_and_revision_history(editing):
    old = editing.signal
    saved = edit(editing, old, cooldown_s=None)
    assert saved['definition']['cooldown_s'] is None
    assert saved['definition']['steps'] == old['definition']['steps']
    assert saved['definition']['trigger'] == old['definition']['trigger']
    assert saved['revision'] == old['revision'] + 1
    assert saved['history'][-1]['definition']['cooldown_s'] == 10
    assert saved['loaded_revision'] is None


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


@pytest.fixture
def visual_editing(editing):
    card = deepcopy(editing.cards['custom'])
    card['unit_id'] = 'vision-device'
    card['observations'] = [
        {'id': 'vision.owner_recognized', 'name': '识别到主人', 'available': False,
         'fields': {'recognized': 'boolean'}},
        {'id': 'vision.stranger_recognized', 'name': '识别到陌生人', 'fields': {'recognized': 'boolean'}},
        {'id': 'vision.person_presence', 'name': '看到人', 'fields': {'detected': 'boolean'}},
        {'id': 'vision.face_presence', 'name': '看到人脸', 'fields': {'detected': 'boolean'}},
        {'id': 'vision.target_presence', 'name': '看到目标物体',
         'fields': {'target': 'string', 'detected': 'boolean'}},
    ]
    card['state_machine']['regions'][0]['transitions'][0]['training_events'] = [
        event['id'] for event in card['observations']]
    editing.cards[card['unit_id']] = card
    definition = deepcopy(editing.signal['definition'])
    definition['steps'][0]['transition_ids'] = [catalog_for(card)['transitions'][0]['id']]
    definition['trigger'] = {'event_id': 'vision.owner_recognized', 'where': {'recognized': True}}
    editing.visual_card = card
    editing.visual = editing.save(card, definition)
    return editing


@pytest.mark.parametrize(('target', 'event_id', 'where'), [
    ('花', 'vision.target_presence', {'target': '花', 'detected': True}),
    ('树', 'vision.target_presence', {'target': '树', 'detected': True}),
    ('盒子', 'vision.target_presence', {'target': '盒子', 'detected': True}),
    ('椅子', 'vision.target_presence', {'target': '椅子', 'detected': True}),
    ('主人', 'vision.owner_recognized', {'recognized': True}),
    ('陌生人', 'vision.stranger_recognized', {'recognized': True}),
    ('人', 'vision.person_presence', {'detected': True}),
    ('人脸', 'vision.face_presence', {'detected': True}),
])
def test_visual_event_target_uses_server_mapping_and_preserves_rule(visual_editing, target, event_id, where):
    env, old = visual_editing, visual_editing.visual
    saved = edit(env, old, event_target=target)
    assert saved['teaching_id'] == old['teaching_id']
    assert saved['revision'] == (1 if target == '主人' else 2)
    assert saved['definition']['trigger'] == {'event_id': event_id, 'where': where}
    for key in ('steps', 'origins', 'cooldown_s', 'situation'):
        assert saved['definition'][key] == old['definition'][key]
    if target != '主人':
        assert saved['history'][0]['definition']['trigger'] == old['definition']['trigger']
        assert saved['loaded_revision'] is None
        assert not service.snapshot(old['unit_id'], env.session)['proposals']


def test_object_target_can_return_to_identity_without_object_identity_evidence(visual_editing):
    env = visual_editing
    flower = edit(env, env.visual, event_target='花')
    owner = edit(env, flower, event_target='主人')
    assert owner['definition']['trigger'] == {
        'event_id': 'vision.owner_recognized', 'where': {'recognized': True}}
    assert owner['revision'] == 3
    with pytest.raises(service.TrainingError, match='revision_conflict'):
        edit(env, flower, event_target='树')


@pytest.mark.parametrize('target', ['', ' ', ' 花', '花 ', '花' * 201, None, True, 1])
def test_event_target_requires_trimmed_bounded_free_text(editing, target):
    with pytest.raises(ValidationError):
        ParameterEdit.model_validate({'expected_revision': 1, 'capability_hash': editing.signal['capability_hash'],
                                     'event_target': target})


def test_target_edit_rejects_mixed_parameters_and_unsupported_observations(editing, visual_editing):
    with pytest.raises(ValidationError):
        edit(visual_editing, visual_editing.visual, event_target='花', event_params={'recognized': False})
    with pytest.raises(service.TrainingError, match='parameter_not_editable'):
        edit(editing, editing.signal, event_target='花')
    with pytest.raises(service.TrainingError, match='parameter_not_editable'):
        edit(editing, editing.blink, event_target='花')
    with pytest.raises(service.TrainingError, match='administrator_required'):
        service.update_parameters(visual_editing.visual['teaching_id'], {
            'expected_revision': 1, 'capability_hash': visual_editing.visual['capability_hash'],
            'event_target': '花'}, {**editing.session, 'actor': 'visitor'})


def test_target_edit_respects_declared_event_capabilities(visual_editing):
    env = visual_editing
    # Keep the saved hash aligned to the deliberately restricted fixture Card;
    # the normal stale-capability boundary is covered separately above.
    env.visual_card['state_machine']['regions'][0]['transitions'][0]['training_events'].remove('vision.target_presence')
    catalog = catalog_for(env.visual_card)
    with store.partition_lock(env.visual['unit_id'], space_id='main', write=True) as data:
        data['records'][env.visual['teaching_id']]['capability_hash'] = catalog['hash']
    record = {**env.visual, 'capability_hash': catalog['hash']}
    with pytest.raises(service.TrainingError, match='invalid_parameter_value'):
        edit(env, record, event_target='花')
    assert store.list_records(record['unit_id'], space_id='main')[0]['revision'] == 1


def test_target_edit_rechecks_conflicts_without_overwriting_original(visual_editing):
    env = visual_editing
    flower_definition = deepcopy(env.visual['definition'])
    flower_definition['trigger'] = {'event_id': 'vision.target_presence', 'where': {'target': '花', 'detected': True}}
    env.save(env.visual_card, flower_definition, '花出现时发出信号')
    with pytest.raises(service.TrainingError, match='training_conflict') as conflict:
        edit(env, env.visual, event_target='花')
    assert conflict.value.details[0]['code'] == 'event_overlap'
    original = next(record for record in store.list_records(env.visual['unit_id'], space_id='main')
                    if record['teaching_id'] == env.visual['teaching_id'])
    assert original['revision'] == 1
    assert original['definition']['trigger']['event_id'] == 'vision.owner_recognized'


def test_target_edit_rejects_negative_observations(visual_editing):
    env = visual_editing
    negative = deepcopy(env.visual['definition'])
    negative['trigger']['where']['recognized'] = False
    saved = env.save(env.visual_card, negative, '没有认出主人时')
    with pytest.raises(service.TrainingError, match='parameter_not_editable'):
        edit(env, saved, event_target='花')
    assert store.list_records(saved['unit_id'], space_id='main')[-1]['revision'] == 1


def test_direct_deletion_retains_identity_history_and_stays_out_of_chat(editing):
    old = editing.signal
    removed = service.remove_record(old['teaching_id'], 1, old['capability_hash'], editing.session)
    assert removed['teaching_id'] == old['teaching_id'] and removed['revision'] == 2
    assert removed['desired_status'] == 'removed'
    assert removed['history'][0]['definition'] == old['definition']
    assert removed['definition'] == old['definition']
    history = service.list_proposals(editing.session, unit_id=old['unit_id'])['proposals']
    assert len(history) == 1 and history[0]['operation'] == 'create'
    assert not service.snapshot(old['unit_id'], editing.session)['proposals']


def test_direct_deletion_rechecks_permission_capability_and_revision(editing):
    old = editing.signal
    for revision, capability, session, reason in [
        (1, old['capability_hash'], {**editing.session, 'actor': 'visitor'}, 'administrator_required'),
        (1, '0' * 64, editing.session, 'capability_changed'),
        (2, old['capability_hash'], editing.session, 'revision_conflict'),
    ]:
        with pytest.raises(service.TrainingError, match=reason):
            service.remove_record(old['teaching_id'], revision, capability, session)
    assert store.list_records(old['unit_id'], space_id='main')[0]['revision'] == 1
