"""Build a deterministic training catalog from device Card declarations."""
from copy import deepcopy
import hashlib
import json


def transition_id(region, edge):
    identity = [region, edge['src'], edge['dst'], edge['trigger'], edge.get('action', ''),
                {key: edge.get('params', {}).get(key) for key in sorted(edge.get('training_fixed_params', []))}]
    return 'tr_' + hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:24]


def catalog_for(card: dict) -> dict:
    machine = card.get('state_machine') or {}
    regions = machine.get('regions', [])
    region_fields = {region['id']: region['state_field'] for region in regions}
    skills = {s.get('invoke', {}).get('action'): s.get('params_schema', {}) for s in card.get('skills', [])}
    fields = {r['state_field']: {'type': 'string', 'enum': list(r['states']),
                                'label': r.get('label', r['state_field']),
                                'labels': {key: (r.get('views', {}).get(key) or {}).get('label', key)
                                           for key in r['states']}}
              for r in regions if r.get('state_field') and r.get('states')}
    fields['requester_is_owner'] = {'type': 'boolean'}
    events = deepcopy(card.get('observations', []))
    transitions = []
    for region in regions:
        for edge in region.get('transitions', []):
            action = edge.get('action') or ''
            # Go2 actions and life transitions have fixed parameters; only
            # parameterized public skills contribute a schema.
            schema = deepcopy(skills.get(action) or {'type': 'object', 'properties': {}, 'additionalProperties': False})
            completions = [candidate['dst'] for candidate in region.get('transitions', [])
                           if candidate['src'] == edge['dst'] and candidate.get('trigger') == 'action_completed']
            completion_state = completions[0] if len(set(completions)) == 1 else edge['dst']
            transitions.append({'id': transition_id(region['id'], edge), 'region': region['id'],
                'src': edge['src'], 'dst': edge['dst'], 'action': action,
                'source_label': (region.get('views', {}).get(edge['src']) or {}).get('label', edge['src']),
                'destination_label': (region.get('views', {}).get(edge['dst']) or {}).get('label', edge['dst']),
                'trigger': edge['trigger'], 'state_field': region['state_field'],
                'completion_state': completion_state,
                'goal_eligible': ((edge.get('completion') == 'joint_posture' and not completions)
                                  or (action == 'LIFE' and edge['src'] != edge['dst'])),
                'parent': ({'state_field': region_fields.get(region['parent']['region']),
                            'state': region['parent']['state']} if region.get('parent') else None),
                'trainable': bool(action and not edge.get('safety') and not edge.get('training_disabled') and action not in ('STOP', 'StopMove')),
                'selector_params': {key: edge.get('params', {}).get(key) for key in edge.get('training_fixed_params', [])},
                'params_schema': schema, 'fixed_params': deepcopy(edge.get('training_fixed_params', [])), 'conditions': list(fields),
                'events': [event_id for event_id in edge.get('training_events', []) if event_id in {e['id'] for e in events}]})
    transitions.sort(key=lambda e: e['id'])
    if len({e['id'] for e in transitions}) != len(transitions):
        raise ValueError('duplicate transition identity')
    result = {'unit_id': card['unit_id'], 'transitions': transitions, 'state_fields': fields,
              'events': events, 'limits': {'cooldown_s': {'minimum': 1, 'maximum': 3600}}}
    result['hash'] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return result


def declare_transition_ids(card: dict) -> dict:
    """Attach stable identities without embedding the derived training catalog."""
    for region in (card.get('state_machine') or {}).get('regions', []):
        for edge in region['transitions']:
            edge['id'] = transition_id(region['id'], edge)
    return card
