"""Validation and evaluation of confirmed, ordered device teachings."""
from copy import deepcopy
import math

from cloud.agents.common.training.models import Definition
from cloud.peers.gateway import _validate_schema


def definition_transition_ids(definition: dict) -> list[str]:
    return list(dict.fromkeys(key for step in definition['steps'] for key in step['transition_ids']))


def _partial_params(schema, params):
    partial = deepcopy(schema)
    partial['required'] = []
    _validate_schema(partial, params)


def _conditions_valid(conditions, catalog):
    for condition in conditions:
        spec = catalog['state_fields'].get(condition['field'])
        values = condition['value'] if condition['op'] == 'in' else [condition['value']]
        if (not spec or not isinstance(values, list) or not values
                or any((spec['type'] == 'boolean' and type(value) is not bool)
                       or (spec['type'] == 'string' and (not isinstance(value, str) or value not in spec.get('enum', [])))
                       for value in values)):
            raise ValueError('unknown condition field or value')


def step_parameters(record: dict, catalog: dict, step_index: int, overrides: dict | None = None,
                    *, transition_id: str | None = None) -> dict:
    """Resolve saved parameters without claiming a current source state."""
    steps = record['definition']['steps']
    if type(step_index) is not int or not 0 <= step_index < len(steps):
        raise ValueError('training_step_invalid')
    step = steps[step_index]
    edges = [edge for edge in catalog['transitions'] if edge['id'] in step['transition_ids']]
    if len(edges) != len(step['transition_ids']):
        raise ValueError('training_transition_unknown')
    first = edges[0] if transition_id is None else next((edge for edge in edges if edge['id'] == transition_id), None)
    if first is None:
        raise ValueError('training_transition_mismatch')
    overrides = overrides or {}
    for edge in edges:
        if set(overrides) & set(edge['fixed_params']):
            raise ValueError('cannot override fixed transition parameters')
        _partial_params(edge['params_schema'], overrides)
    params = {**step['defaults'], **first['selector_params'], **overrides}
    for edge in edges:
        _validate_schema(edge['params_schema'], params)
    return {'step_index': step_index, 'action': first['action'], 'params': params,
            'transition_ids': list(step['transition_ids']), 'goal_state': step.get('goal_state'),
            'conditions': deepcopy(step['conditions']), 'defaults': deepcopy(step['defaults'])}


def validate_definition(definition: dict, catalog: dict) -> dict:
    definition = Definition.model_validate(definition).model_dump()
    if len(set(definition['origins'])) != len(definition['origins']):
        raise ValueError('duplicate origin')
    trigger = definition['trigger']
    if trigger and 'agent_autonomous' not in definition['origins']:
        raise ValueError('event_trigger_requires_agent_autonomous')
    if not trigger and definition.get('situation') and any(
            condition['field'] == 'requester_is_owner'
            for step in definition['steps'] for condition in step['conditions']):
        raise ValueError('requester_guard_requires_global_scope: keep the owner condition and use situation=null; the transition source state already scopes wake from rest')
    edges = {edge['id']: edge for edge in catalog['transitions']}
    previous_states = {}
    for index, step in enumerate(definition['steps']):
        ids = step['transition_ids']
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate transition')
        selected = [edges.get(key) for key in ids]
        if any(edge is None or not edge['trainable'] for edge in selected):
            raise ValueError('unknown or untrainable transition')
        first = selected[0]
        goal = step.get('goal_state')
        if any((edge['state_field'], edge['selector_params']) !=
               (first['state_field'], first['selector_params']) for edge in selected):
            raise ValueError('step alternatives require the same state region and fixed parameters')
        if goal is None and any(edge['action'] != first['action'] for edge in selected):
            raise ValueError('a command step requires one action with equivalent source-state alternatives')
        parent = first.get('parent')
        if parent and previous_states.get(parent['state_field']) not in (None, {parent['state']}):
            raise ValueError('step_parent_state_unavailable: restore the parent lifecycle state explicitly')
        for edge in selected:
            if set(step['defaults']) & set(edge['fixed_params']):
                raise ValueError('cannot override fixed transition parameters')
            _partial_params(edge['params_schema'], step['defaults'])
            if trigger or len(definition['steps']) > 1:
                # Autonomous and ordered behaviors cannot ask a later raw
                # caller to invent missing action parameters.
                _validate_schema(edge['params_schema'], {**step['defaults'], **edge['selector_params']})
        _conditions_valid(step['conditions'], catalog)
        if goal is not None and any(not edge.get('goal_eligible') or edge['dst'] != goal for edge in selected):
            raise ValueError('goal_state must be the explicit stable destination of this step')
        field = first['state_field']
        known = previous_states.get(field)
        sources = {edge['src'] for edge in selected}
        if known is not None and any(state not in sources and state != goal for state in known):
            raise ValueError('step_source_state_unreachable: explicitly include any required preparation action')
        previous_states[field] = {edge.get('completion_state', edge['dst']) for edge in selected}
        if trigger and index == 0:
            event = next((item for item in catalog['events'] if item['id'] == trigger['event_id']), None)
            if event is None or any(trigger['event_id'] not in edge['events'] for edge in selected):
                raise ValueError('unknown event for the first step')
            for key, value in trigger['where'].items():
                kind = event['fields'].get(key)
                if (kind is None or (kind == 'boolean' and type(value) is not bool)
                        or (kind == 'string' and not isinstance(value, str))
                        or (kind == 'integer' and type(value) is not int)
                        or (kind == 'number' and (type(value) not in (int, float) or not math.isfinite(value)))):
                    raise ValueError('invalid event field')
    cooldown = definition['cooldown_s']
    limits = catalog['limits']['cooldown_s']
    if cooldown is not None and (not math.isfinite(cooldown) or not limits['minimum'] <= cooldown <= limits['maximum']):
        raise ValueError('cooldown out of range')
    return definition


def conflicts(definition: dict, records: list[dict]) -> list[dict]:
    found = []
    for record in records:
        if record.get('desired_status') != 'enabled':
            continue
        other = record['definition']
        same_scope = definition.get('situation') == other.get('situation')
        a, b = definition['trigger'], other['trigger']
        if not same_scope or not set(definition['origins']) & set(other['origins']):
            continue
        if a and b and a['event_id'] == b['event_id'] and set(definition['steps'][0]['transition_ids']) & set(other['steps'][0]['transition_ids']):
            found.append({'code': 'event_overlap', 'teaching_id': record['teaching_id']})
        if a or b:
            continue
        for step in definition['steps']:
            for previous in other['steps']:
                if set(step['transition_ids']) & set(previous['transition_ids']) and any(
                        key in previous['defaults'] and value != previous['defaults'][key]
                        for key, value in step['defaults'].items()):
                    found.append({'code': 'default_conflict', 'teaching_id': record['teaching_id']})
    return found


def evaluate_conditions(conditions, facts, *, now):
    reasons = []
    for condition in conditions:
        field = condition['field']
        if field == 'requester_is_owner':
            requester = facts.get('requester') or {}
            value = requester.get('is_owner') if requester.get('verified') is True and requester.get('expires_at', 0) > now else None
        else:
            value = facts.get('state', {}).get(field) if facts.get('state_fresh') is True else None
        if value is None:
            reasons.append({'code': 'condition_unknown', 'field': field})
        elif not any(type(value) is type(candidate) and value == candidate for candidate in
                     (condition['value'] if condition['op'] == 'in' else [condition['value']])):
            reasons.append({'code': 'condition_unsatisfied', 'field': field})
    return reasons


def evaluate(records: list[dict], request: dict, facts: dict, *, now: float) -> dict:
    reasons, refs, defaults, selected_defaults = [], [], {}, {}
    for record in records:
        if record.get('desired_status') != 'enabled' or record.get('loaded_revision') != record.get('revision'):
            continue
        definition = record['definition']
        if request['origin'] not in definition['origins']:
            continue
        selected = record['teaching_id'] in {request.get('training_ref'), request.get('trigger_ref')}
        if definition['trigger'] and request.get('trigger_ref') != record['teaching_id']:
            continue
        if definition.get('situation') and not selected:
            continue
        step_index = request.get('step_index') if selected else None
        matching = [(index, step) for index, step in enumerate(definition['steps'])
                    if request['transition_id'] in step['transition_ids'] and (step_index is None or index == step_index)]
        if not matching:
            continue
        if definition['trigger'] and definition.get('situation') and request.get('trigger_situation_confirmed') is not True:
            reasons.append({'code': 'situation_not_confirmed'})
        refs.append({'teaching_id': record['teaching_id'], 'revision': record['revision']})
        for index, step in matching:
            reasons.extend(evaluate_conditions(step['conditions'], facts, now=now))
            cooldown = definition['cooldown_s'] if index == 0 else None
            last = facts.get('last_completed_at', {}).get(request['transition_id'])
            if cooldown is not None and facts.get('completion_history_known') is not True:
                reasons.append({'code': 'completion_history_unknown'})
            elif cooldown is not None and last is not None and now - last < cooldown:
                reasons.append({'code': 'cooldown', 'remaining_s': cooldown - (now - last)})
            if selected:
                selected_defaults.update(step['defaults'])
            else:
                for key, value in step['defaults'].items():
                    if key in defaults and defaults[key] != value:
                        reasons.append({'code': 'default_conflict', 'field': key})
                    defaults[key] = value
    return {'allowed': not reasons, 'params': {**defaults, **selected_defaults, **request['params']},
            'reasons': reasons, 'teaching_refs': refs}


def current_state_matches(edge, facts):
    return (facts.get('state') or {}).get(edge['state_field']) == edge['src']


def training_event_candidates(event, records, catalog, facts, *, now) -> list[dict]:
    ts = event.get('ts')
    if (type(ts) not in (int, float) or not math.isfinite(ts)
            or not 0 <= now - ts <= 5 or not event.get('event_id') or facts.get('state_fresh') is not True):
        return []
    matches = []
    for record in records:
        definition = record['definition']
        trigger = definition['trigger']
        if (record.get('desired_status') != 'enabled' or record.get('loaded_revision') != record.get('revision')
                or record.get('capability_hash') != catalog['hash'] or not trigger
                or 'agent_autonomous' not in definition['origins'] or trigger['event_id'] != event.get('id')
                or any((event.get('values') or {}).get(key) != value for key, value in trigger['where'].items())):
            continue
        first = definition['steps'][0]
        choices = [edge for edge in catalog['transitions'] if edge['id'] in first['transition_ids'] and edge['trainable']]
        edge = next((edge for edge in choices if current_state_matches(edge, facts)), None)
        goal_observed = bool(first.get('goal_state') and choices
                             and facts['state'].get('connected') is True
                             and all(choice.get('goal_eligible') and
                                     facts['state'].get(choice['state_field']) == first['goal_state']
                                     for choice in choices))
        if edge or goal_observed:
            prepared = step_parameters(record, catalog, 0, transition_id=edge['id'] if edge else None)
            matches.append({'teaching_id': record['teaching_id'], 'revision': record['revision'],
                            'transition_id': edge['id'] if edge else None, 'event_id': event['event_id'],
                            'situation': definition.get('situation'), 'steps': deepcopy(definition['steps']),
                            'name': record.get('name', ''), 'source_text': record.get('source_text', ''),
                            'params': prepared['params']})
    return matches


def training_event_intent(event, records, catalog, facts, *, now):
    matches = training_event_candidates(event, records, catalog, facts, now=now)
    return matches[0] if len(matches) == 1 else None
