"""Test-only records derived from the real device declarations."""
from cloud.agents.common.training.catalog import catalog_for


def _record(catalog, edge_ids, *, defaults=None, cooldown=None):
    return {'teaching_id': 'test-teaching', 'unit_id': catalog['unit_id'], 'space_id': 'main',
            'name': 'test teaching', 'source_text': 'test', 'capability_hash': catalog['hash'],
            'history': [], 'created_at': 0, 'updated_at': 0, 'schema_version': 1, 'revision': 1, 'loaded_revision': 1,
            'desired_status': 'enabled', 'definition': {'transition_ids': edge_ids,
            'origins': ['user_delegated', 'agent_autonomous'], 'conditions': [],
            'cooldown_s': cooldown, 'defaults': defaults or {}, 'trigger': None}}


def hello_cooldown(seconds):
    from cloud.go2.card import _build_go2_card
    catalog = catalog_for(_build_go2_card())
    edge = next(e for e in catalog['transitions'] if e['trigger'] == 'Hello' and e['src'] == 'standing')
    return _record(catalog, [edge['id']], cooldown=seconds)


def blink_defaults(count):
    from cloud.cards.builder import build_card_from_manifest
    from edge.lamp_contract import FSM_VERSION, capabilities
    card = build_card_from_manifest({'name': '测试灯', 'firmware_ver': '0.3.0', 'unit_id': 'test_led', 'agent_type': 'actuator', 'fsm': 'led',
                                     'fsm_version': FSM_VERSION, 'capabilities': capabilities()})
    catalog = catalog_for(card)
    edges = [e['id'] for e in catalog['transitions'] if e['action'] == 'BLINK' and e['trainable']]
    return _record(catalog, edges, defaults={'count': count})


def training_record():
    """Complete disabled Hello record for storage/API tests."""
    record = hello_cooldown(60)
    record.update(desired_status='disabled', loaded_revision=None)
    return record


def person_greeting_fixture():
    record = hello_cooldown(60)
    from cloud.go2.card import _build_go2_card
    catalog = catalog_for(_build_go2_card())
    record['definition']['trigger'] = {'event_id': 'vision.person_presence', 'where': {'detected': True}, 'params': {}}
    return {'catalog': catalog, 'records': [record], 'event': {'id': 'vision.person_presence', 'event_id': 'frame:1', 'ts': 100, 'values': {'detected': True}}, 'facts': {'state_fresh': True, 'state': {'body_state': 'standing'}}}


def lamp_sound_fixture():
    record = blink_defaults(2)
    from cloud.cards.builder import build_card_from_manifest
    from edge.lamp_contract import FSM_VERSION, capabilities
    card = build_card_from_manifest({'name': '测试灯', 'firmware_ver': '0.3.0', 'unit_id': 'test_led', 'agent_type': 'actuator', 'fsm': 'led', 'fsm_version': FSM_VERSION, 'capabilities': capabilities()})
    catalog = catalog_for(card)
    edge = next(e for e in catalog['transitions'] if e['action'] == 'BLINK' and e['src'] == 'OFF')
    record['definition'].update(transition_ids=[edge['id']], defaults={}, trigger={'event_id': 'sensor.sound_detected', 'where': {'detected': True}, 'params': {'count': 2}})
    return {'catalog': catalog, 'records': [record], 'event': {'id': 'sensor.sound_detected', 'event_id': 'sound:100', 'ts': 100, 'values': {'detected': True}}, 'facts': {'state_fresh': True, 'state': {'ism': 'OFF'}}}
