"""Training validation and evaluation; caller supplies trusted facts and time."""
from copy import deepcopy
import math
from cloud.agents.common.training.models import Definition
from cloud.peers.gateway import _validate_schema


def _partial_params(schema, params):
    partial = deepcopy(schema)
    partial['required'] = []
    _validate_schema(partial, params)


def validate_definition(definition: dict, catalog: dict) -> dict:
    definition = Definition.model_validate(definition).model_dump(exclude_unset=True)
    edges = {e['id']: e for e in catalog['transitions']}
    ids = definition['transition_ids']
    if len(set(ids)) != len(ids) or len(set(definition['origins'])) != len(definition['origins']):
        raise ValueError('duplicate transition or origin')
    for edge_id in ids:
        edge = edges.get(edge_id)
        if not edge or not edge['trainable']:
            raise ValueError('unknown or untrainable transition')
        if set(definition['defaults']) & set(edge['fixed_params']):
            raise ValueError('cannot override fixed transition parameters')
        _partial_params(edge['params_schema'], definition['defaults'])
        if definition['trigger']:
            if set(definition['trigger']['params']) & set(edge['fixed_params']):
                raise ValueError('cannot override fixed transition parameters')
            _partial_params(edge['params_schema'], definition['trigger']['params'])
    for condition in definition['conditions']:
        spec = catalog['state_fields'].get(condition['field'])
        value = condition['value']
        values = value if condition['op'] == 'in' else [value]
        if not spec or not isinstance(values, list) or not values or any((spec['type'] == 'boolean' and type(v) is not bool) or (spec['type'] == 'string' and (not isinstance(v, str) or v not in spec.get('enum', []))) for v in values):
            raise ValueError('unknown condition field or value')
    cooldown = definition['cooldown_s']
    limits = catalog['limits']['cooldown_s']
    if cooldown is not None and (not math.isfinite(cooldown) or not limits['minimum'] <= cooldown <= limits['maximum']):
        raise ValueError('cooldown out of range')
    trigger = definition['trigger']
    if trigger:
        if len(ids) != 1:
            raise ValueError('event trigger requires one transition')
        event = next((e for e in catalog['events'] if e['id'] == trigger['event_id']), None)
        if not event or trigger['event_id'] not in edges[ids[0]]['events']:
            raise ValueError('unknown event')
        for key, value in trigger['where'].items():
            kind = event['fields'].get(key)
            if kind is None or (kind == 'boolean' and type(value) is not bool) or (kind == 'string' and not isinstance(value, str)) or (kind == 'integer' and type(value) is not int) or (kind == 'number' and (type(value) not in (int, float) or not math.isfinite(value))):
                raise ValueError('invalid event field')
    return definition


def application_parameters(record: dict, catalog: dict, overrides: dict) -> tuple[str, dict]:
    """Resolve one saved action across its legal source-state transitions."""
    definition = validate_definition(record['definition'], catalog)
    if definition['trigger']:
        raise ValueError('event_training_requires_event')
    edges = [edge for edge in catalog['transitions']
             if edge['id'] in definition['transition_ids']]
    first = edges[0]
    action, fixed = first['action'], first['selector_params']
    if any(edge['action'] != action or edge['selector_params'] != fixed for edge in edges):
        raise ValueError('training_action_ambiguous')
    for edge in edges:
        if set(overrides) & set(edge['fixed_params']):
            raise ValueError('cannot override fixed transition parameters')
        _partial_params(edge['params_schema'], overrides)
    params = {**definition['defaults'], **fixed, **overrides}
    for edge in edges:
        _validate_schema(edge['params_schema'], params)
    return action, params


def conflicts(definition: dict, records: list[dict]) -> list[dict]:
    found = []
    for record in records:
        if record.get('desired_status') != 'enabled':
            continue
        other = record['definition']
        if not set(definition['transition_ids']) & set(other['transition_ids']):
            continue
        a, b = definition['trigger'], other['trigger']
        # Situational configurations are selected explicitly by the role Graph.
        same_scope = definition.get('situation') == other.get('situation')
        if same_scope and a and b and a['event_id'] == b['event_id']:
            found.append({'code': 'event_overlap', 'teaching_id': record['teaching_id']})
        if same_scope and (not a or not b or a['event_id'] == b['event_id']) and set(definition['origins']) & set(other['origins']):
            if any(k in other['defaults'] and v != other['defaults'][k] for k, v in definition['defaults'].items()):
                found.append({'code': 'default_conflict', 'teaching_id': record['teaching_id']})
    return found


def evaluate(records: list[dict], request: dict, facts: dict, *, now: float) -> dict:
    reasons, refs, defaults, selected_defaults = [], [], {}, {}
    for record in records:
        if record.get('desired_status') != 'enabled' or record.get('loaded_revision') != record.get('revision'):
            continue
        d = record['definition']
        if request['transition_id'] not in d['transition_ids'] or request['origin'] not in d['origins']:
            continue
        if d['trigger'] and request.get('trigger_ref') != record['teaching_id']:
            continue
        if (d.get('situation') and not d['trigger']
                and request.get('training_ref') != record['teaching_id']):
            continue
        if (d.get('situation') and d['trigger']
                and request.get('trigger_situation_confirmed') is not True):
            reasons.append({'code': 'situation_not_confirmed'})
        refs.append({'teaching_id': record['teaching_id'], 'revision': record['revision']})
        for condition in d['conditions']:
            field = condition['field']
            if field == 'requester_is_owner':
                requester = facts.get('requester') or {}
                value = requester.get('is_owner') if requester.get('verified') is True and requester.get('expires_at', 0) > now else None
            else:
                value = facts.get('state', {}).get(field) if facts.get('state_fresh') is True else None
            if value is None:
                reasons.append({'code': 'condition_unknown', 'field': field})
            elif not any(type(value) is type(v) and value == v for v in (condition['value'] if condition['op'] == 'in' else [condition['value']])):
                reasons.append({'code': 'condition_unsatisfied', 'field': field})
        cooldown = d['cooldown_s']
        last = facts.get('last_completed_at', {}).get(request['transition_id'])
        if cooldown is not None and facts.get('completion_history_known') is not True:
            reasons.append({'code': 'completion_history_unknown'})
        elif cooldown is not None and last is not None and now - last < cooldown:
            reasons.append({'code': 'cooldown', 'remaining_s': cooldown - (now - last)})
        if record['teaching_id'] == request.get('training_ref'):
            selected_defaults.update(d['defaults'])
            continue
        for key, value in d['defaults'].items():
            if key in defaults and defaults[key] != value:
                reasons.append({'code': 'default_conflict', 'field': key})
            defaults[key] = value
    return {'allowed': not reasons,
            'params': {**defaults, **selected_defaults, **request['params']},
            'reasons': reasons, 'teaching_refs': refs}


def training_event_candidates(event, records, catalog, facts, *, now) -> list[dict]:
    """Find current event candidates; their semantic situations still need the Graph."""
    ts = event.get('ts')
    if (type(ts) not in (int, float) or not math.isfinite(ts)
            or not 0 <= now - ts <= 5 or not event.get('event_id')
            or facts.get('state_fresh') is not True):
        return []
    matches = []
    for record in records:
        definition = record.get('definition') or {}
        trigger = definition.get('trigger')
        if (record.get('desired_status') != 'enabled'
                or record.get('loaded_revision') != record.get('revision')
                or record.get('capability_hash') != catalog['hash'] or not trigger
                or 'agent_autonomous' not in definition.get('origins', [])
                or trigger['event_id'] != event.get('id')
                or any((event.get('values') or {}).get(k) != v for k, v in trigger['where'].items())):
            continue
        edge = next((e for e in catalog['transitions'] if e['id'] in definition['transition_ids']
                     and e['trainable'] and current_state_matches(e, facts)), None)
        if edge:
            matches.append({'teaching_id': record['teaching_id'], 'revision': record['revision'],
                            'transition_id': edge['id'], 'event_id': event['event_id'],
                            'situation': definition.get('situation'),
                            'name': record.get('name', ''), 'source_text': record.get('source_text', ''),
                            'params': {**edge['selector_params'], **trigger['params']}})
    return matches


def training_event_intent(event, records, catalog, facts, *, now):
    """Compatibility projection for callers that only support an unambiguous event."""
    matches = training_event_candidates(event, records, catalog, facts, now=now)
    return matches[0] if len(matches) == 1 else None


def current_state_matches(edge, facts):
    return (facts.get('state') or {}).get(edge['state_field']) == edge['src']
