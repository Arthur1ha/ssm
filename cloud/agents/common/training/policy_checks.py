from cloud.agents.common.training.policy import evaluate
from cloud.agents.common.training.check_fixtures import hello_cooldown, blink_defaults


def test_confirmed_completion_controls_cooldown():
    record = hello_cooldown(60)
    edge = record['definition']['transition_ids'][0]
    request = dict(transition_id=edge, origin='user_delegated', params={})
    facts = dict(state={'body_state': 'standing'}, state_fresh=True,
                 last_completed_at={edge: 100.0}, completion_history_known=True)
    assert evaluate([record], request, facts, now=130)['allowed'] is False
    assert evaluate([record], request, facts, now=160)['allowed'] is True


def test_explicit_count_overrides_trained_default():
    record = blink_defaults(2)
    request = dict(transition_id=record['definition']['transition_ids'][0],
                   origin='user_delegated', params={'count': 5})
    facts = dict(state={'ism': 'OFF'}, state_fresh=True,
                 last_completed_at={}, completion_history_known=True)
    assert evaluate([record], request, facts, now=100)['params']['count'] == 5

from copy import deepcopy
import pytest
from cloud.agents.common.training.catalog import catalog_for
from cloud.agents.common.training.policy import conflicts, validate_definition


def _go2_catalog():
    from cloud.go2.card import _build_go2_card
    return catalog_for(_build_go2_card())


def _request(record):
    return {'transition_id': record['definition']['transition_ids'][0], 'origin': 'user_delegated', 'params': {}}


def _facts():
    return {'state': {'body_state': 'standing'}, 'state_fresh': True,
            'last_completed_at': {}, 'completion_history_known': True}


def test_unknown_completion_history_blocks_but_known_first_allows():
    record = hello_cooldown(60)
    facts = _facts()
    assert evaluate([record], _request(record), facts, now=100)['allowed']
    facts['completion_history_known'] = False
    assert evaluate([record], _request(record), facts, now=100)['reasons'][0]['code'] == 'completion_history_unknown'


def test_owner_and_unknown_identity():
    record = hello_cooldown(1)
    record['definition']['conditions'] = [{'field': 'requester_is_owner', 'op': 'eq', 'value': True}]
    for requester, allowed in [({'verified': True, 'is_owner': True, 'expires_at': 101}, True),
                               ({'verified': True, 'is_owner': False, 'expires_at': 101}, False),
                               ({'verified': True, 'is_owner': True, 'expires_at': 99}, False),
                               ({}, False)]:
        facts = _facts() | {'requester': requester}
        assert evaluate([record], _request(record), facts, now=100)['allowed'] is allowed


def test_trigger_scope_does_not_restrict_manual_and_regular_guard_still_applies():
    event = hello_cooldown(60)
    event['definition']['trigger'] = {'event_id': 'vision.person_presence', 'where': {'detected': True}, 'params': {}}
    facts = _facts()
    facts['completion_history_known'] = False
    assert evaluate([event], _request(event), facts, now=100)['allowed']
    assert not evaluate([event], _request(event) | {'trigger_ref': event['teaching_id']}, facts, now=100)['allowed']


def test_stale_and_missing_state_deny():
    record = hello_cooldown(1)
    record['definition']['conditions'] = [{'field': 'body_state', 'op': 'in', 'value': ['standing']}]
    for facts in [_facts() | {'state_fresh': False}, _facts() | {'state': {}}]:
        assert evaluate([record], _request(record), facts, now=100)['reasons'][0]['code'] == 'condition_unknown'


def test_validation_rejects_illegal_fields_and_automatic_edges():
    catalog = _go2_catalog()
    definition = hello_cooldown(60)['definition']
    assert validate_definition(definition, catalog) == definition
    invalid = deepcopy(definition)
    invalid['conditions'] = [{'field': 'invented', 'op': 'eq', 'value': True}]
    with pytest.raises(ValueError): validate_definition(invalid, catalog)
    invalid = deepcopy(definition)
    invalid['transition_ids'] = [next(e['id'] for e in catalog['transitions'] if not e['trainable'])]
    with pytest.raises(ValueError): validate_definition(invalid, catalog)
    with pytest.raises(ValueError): validate_definition(definition | {'unexpected': 1}, catalog)


def test_default_and_event_conflicts():
    record = blink_defaults(2)
    other = deepcopy(record['definition'])
    other['defaults']['count'] = 3
    assert conflicts(other, [record])[0]['code'] == 'default_conflict'
    other['trigger'] = {'event_id': 'sensor.sound_detected', 'where': {'detected': True}, 'params': {}}
    record['definition']['trigger'] = deepcopy(other['trigger'])
    assert conflicts(other, [record])[0]['code'] == 'event_overlap'


def test_catalog_id_and_hash_independent_of_order_and_layout():
    from cloud.go2.card import _build_go2_card
    card = _build_go2_card()
    original = catalog_for(card)
    for region in card['state_machine']['regions']:
        region['transitions'].reverse()
        region['layout'] = {}
    assert catalog_for(card) == original


def test_lamp_schema_and_cooldown_limits():
    from cloud.cards.builder import build_card_from_manifest
    from edge.lamp_contract import FSM_VERSION, capabilities
    catalog = catalog_for(build_card_from_manifest({'name': '测试灯', 'firmware_ver': '0.3.0', 'unit_id': 'led', 'agent_type': 'actuator', 'fsm': 'led', 'fsm_version': FSM_VERSION, 'capabilities': capabilities()}))
    definition = blink_defaults(2)['definition']
    assert validate_definition(definition, catalog) == definition
    invalid = deepcopy(definition)
    invalid['defaults']['count'] = 1000
    with pytest.raises(ValueError): validate_definition(invalid, catalog)
    invalid = deepcopy(definition)
    invalid['cooldown_s'] = 3601
    with pytest.raises(ValueError): validate_definition(invalid, catalog)


def test_events_must_be_declared_for_target_edge():
    catalog = _go2_catalog()
    definition = hello_cooldown(60)['definition']
    definition['trigger'] = {'event_id': 'vision.person_presence', 'where': {'detected': True}, 'params': {}}
    assert validate_definition(definition, catalog) == definition
    definition['trigger']['where'] = {'invented': True}
    with pytest.raises(ValueError): validate_definition(definition, catalog)
    definition['trigger'] = {'event_id': 'vision.face_presence', 'where': {'detected': True}, 'params': {}}
    assert validate_definition(definition, catalog) == definition
    definition['trigger']['event_id'] = 'vision.invented'
    with pytest.raises(ValueError): validate_definition(definition, catalog)


@pytest.mark.parametrize('action', ['Sit', 'Stretch', 'FingerHeart', 'RiseSit'])
@pytest.mark.parametrize('event_id,where', [
    ('vision.person_presence', {'detected': True}),
    ('vision.face_presence', {'detected': True}),
    ('vision.owner_recognized', {'recognized': True}),
])
def test_observations_bind_existing_trainable_actions(action, event_id, where):
    catalog = _go2_catalog()
    definition = hello_cooldown(60)['definition']
    definition['transition_ids'] = [edge['id'] for edge in catalog['transitions']
                                    if edge['action'] == action and edge['trainable']]
    definition['trigger'] = {'event_id': event_id, 'where': where, 'params': {}}
    assert validate_definition(definition, catalog) == definition
    with pytest.raises(ValueError, match='event_trigger_requires_agent_autonomous'):
        validate_definition(definition | {'origins': ['user_delegated']}, catalog)


def test_event_cannot_bind_two_different_actions():
    catalog = _go2_catalog()
    definition = hello_cooldown(60)['definition']
    definition['transition_ids'] = [edge['id'] for edge in catalog['transitions']
                                    if edge['action'] in {'Sit', 'Stretch'}]
    definition['trigger'] = {'event_id': 'vision.owner_recognized', 'where': {'recognized': True}, 'params': {}}
    with pytest.raises(ValueError, match='one action'):
        validate_definition(definition, catalog)


def test_card_edge_ids_match_catalog_and_unloaded_teaching_is_inactive():
    from cloud.go2.card import _build_go2_card
    card = _build_go2_card()
    body = next(r for r in card['state_machine']['regions'] if r['id'] == 'body')
    hello = next(e for e in body['transitions'] if e['trigger'] == 'Hello')
    assert hello['id'] in {e['id'] for e in catalog_for(card)['transitions']}
    record = hello_cooldown(60)
    record['loaded_revision'] = 0
    facts = _facts() | {'completion_history_known': False}
    assert evaluate([record], _request(record), facts, now=100)['allowed']


def test_safety_recovery_and_fixed_arguments():
    from cloud.agents.common.training.catalog import transition_id
    from cloud.cards.builder import build_card_from_manifest
    from edge.lamp_contract import FSM_VERSION, capabilities
    lamp = catalog_for(build_card_from_manifest({'name': '测试灯', 'firmware_ver': '0.3.0', 'unit_id': 'led', 'agent_type': 'actuator', 'fsm': 'led', 'fsm_version': FSM_VERSION, 'capabilities': capabilities()}))
    assert all(not e['trainable'] for e in lamp['transitions'] if e['src'] == 'ERROR')
    assert all(not e['trainable'] for e in _go2_catalog()['transitions'] if e['trigger'] in ('stop_explore', 'StopMove', 'EMERGENCY_STOP'))
    edge = {'src': 'a', 'dst': 'b', 'trigger': 'set', 'action': 'set', 'params': {'state': 'x'}, 'training_fixed_params': ['state']}
    other = deepcopy(edge)
    other['params']['state'] = 'y'
    assert transition_id('r', edge) != transition_id('r', other)


def test_lamp_transition_rejects_fixed_state_override():
    from cloud.cards.builder import build_card_from_manifest
    from edge.lamp_contract import capabilities
    card = build_card_from_manifest({'name': '测试灯', 'firmware_ver': '0.3.0', 'unit_id': 'led', 'agent_type': 'actuator',
                                     'fsm': 'led', 'fsm_version': 3, 'capabilities': capabilities()})
    catalog = catalog_for(card)
    edge = next(e for e in catalog['transitions'] if e['src'] == 'OFF' and e['dst'] == 'ON' and e['action'] == 'SET_STATE')
    assert edge['fixed_params'] == ['state']
    definition = {'transition_ids': [edge['id']], 'origins': ['user_delegated'],
                  'conditions': [], 'cooldown_s': None, 'defaults': {'state': 'OFF'}, 'trigger': None}
    with pytest.raises(ValueError, match='fixed transition parameters'):
        validate_definition(definition, catalog)
