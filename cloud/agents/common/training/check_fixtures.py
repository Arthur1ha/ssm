"""Current v2 test records derived from the real device declarations."""
from cloud.agents.common.training.catalog import catalog_for


def person_greeting_fixture():
    from cloud.go2.card import _build_go2_card
    catalog = catalog_for(_build_go2_card())
    edge = next(e for e in catalog['transitions'] if e['trigger'] == 'Hello' and e['src'] == 'standing')
    record = {
        'teaching_id': 'test-teaching', 'unit_id': catalog['unit_id'], 'space_id': 'main',
        'name': 'test teaching', 'source_text': 'test', 'capability_hash': catalog['hash'],
        'history': [], 'created_at': 0, 'updated_at': 0, 'schema_version': 2,
        'revision': 1, 'loaded_revision': 1, 'desired_status': 'enabled',
        'definition': {
            'steps': [{'transition_ids': [edge['id']], 'conditions': [], 'defaults': {}}],
            'origins': ['user_delegated', 'agent_autonomous'], 'cooldown_s': 60,
            'trigger': {'event_id': 'vision.person_presence', 'where': {'detected': True}},
            'situation': None,
        },
    }
    return {'catalog': catalog, 'records': [record], 'event': {'id': 'vision.person_presence', 'event_id': 'frame:1', 'ts': 100, 'values': {'detected': True}}, 'facts': {'state_fresh': True, 'state': {'body_state': 'standing'}}}
