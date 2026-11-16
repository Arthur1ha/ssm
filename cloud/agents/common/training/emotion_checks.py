"""Emotion teachings reuse confirmed definitions and the existing action boundary."""

from copy import deepcopy
from dataclasses import asdict, replace
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cloud.agents.common.emotions import EMOTIONS
from cloud.agents.common.runner import GraphRun
from cloud.agents.common.training import context, policy, service, store
from cloud.agents.common.training.catalog import catalog_for
from cloud.agents.common.training.models import Definition
from cloud.agents.common.training.tools import (
    ProposeTraining, _preserve_event_request, prepare_training_application,
)
from cloud.esp32.capability_chain_checks import card as lamp_card
from cloud.space.execution import ExecutionContext


@pytest.fixture
def teaching(monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path / 'training'))
    card = lamp_card()
    catalog = catalog_for(card)
    monkeypatch.setattr(service, '_targets', lambda space: {card['unit_id']: card} if space == 'main' else {})
    monkeypatch.setattr(service, '_emit', lambda *_a, **_k: None)
    session = {'session_id': 'emotion-training', 'space_id': 'main', 'actor': 'admin',
               'expires_at': time.time() + 300,
               'training_source': service.UserText('emotion-training', '以后开心时亮紫色')}
    definition = {'steps': [{'transition_ids': [edge['id'] for edge in catalog['transitions']
                         if edge['action'] == 'SET_COLOR'],
                         'defaults': {'r': 160, 'g': 50, 'b': 255, 'brightness': 20}}],
                  'origins': ['user_delegated', 'agent_autonomous'], 'emotion': 'happy'}

    def save(value=None, *, confirmed=True):
        proposal = service.propose({'operation': 'create', 'unit_id': card['unit_id'],
            'name': '开心用紫色', 'definition': deepcopy(value or definition),
            'expected_revision': 0, 'capability_hash': catalog['hash']}, session)
        return service.confirm(proposal['proposal_id'], 0, session) if confirmed else proposal

    run = GraphRun('expression-run', 'conversation', '你好', time.time() + 60,
                   agent_id=card['unit_id'], emotion='happy')
    return SimpleNamespace(card=card, catalog=catalog, definition=definition,
                           session=session, save=save, run=run)


def select(run, **kwargs):
    return context.emotion_training_call(run, space_id='main', origin_kind='user_delegated', **kwargs)


def test_emotion_schema_reuses_reply_enum_and_preserves_old_records(teaching):
    legacy = deepcopy(teaching.definition)
    legacy.pop('emotion')
    assert Definition.model_validate(legacy).emotion is None
    schema = ProposeTraining.model_json_schema()['$defs']['Definition']['properties']['emotion']
    assert schema['anyOf'][0]['enum'] == list(EMOTIONS)
    for invalid in ('excited', '', 1, {}, True):
        with pytest.raises(ValidationError):
            Definition.model_validate(teaching.definition | {'emotion': invalid})


def test_mapping_is_confirmed_versioned_loaded_and_revocable(teaching):
    draft = teaching.save(confirmed=False)
    assert select(teaching.run) is None
    assert '表达开心时' in draft['scopeText']
    saved = service.confirm(draft['proposal_id'], 0, teaching.session)
    expected = {'unit_id': teaching.card['unit_id'], 'teaching_id': saved['teaching_id'],
                'revision': 1, 'overrides': {}}
    assert select(teaching.run) == expected
    assert saved['loaded_revision'] is None
    service.acknowledge(saved['unit_id'], {saved['teaching_id']: {
        'revision': 1, 'capability_hash': teaching.catalog['hash']}},
        service.ExecutorContext('lamp', 'main'))
    assert store.list_records(saved['unit_id'], space_id='main')[0]['loaded_revision'] == 1
    changed = deepcopy(saved['definition'])
    changed['emotion'] = 'relaxed'
    proposal = service.propose({'operation': 'update', 'unit_id': saved['unit_id'],
        'teaching_id': saved['teaching_id'], 'name': '放松用紫色', 'definition': changed,
        'expected_revision': 1, 'capability_hash': teaching.catalog['hash']}, teaching.session)
    updated = service.confirm(proposal['proposal_id'], 1, teaching.session)
    assert updated['revision'] == 2 and select(teaching.run) is None
    teaching.run.emotion = 'relaxed'
    assert select(teaching.run) == expected | {'revision': 2}
    service.set_enabled(updated['teaching_id'], False, 2, teaching.session)
    assert select(teaching.run) is None


@pytest.mark.parametrize('change', ['situation', 'origin', 'capability', 'already_applied', 'different_agent'])
def test_mapping_selection_does_not_infer_context_or_repeat_actions(teaching, change):
    definition = deepcopy(teaching.definition)
    if change == 'situation':
        definition['situation'] = '仅当用户夸奖我时'
    if change == 'origin':
        definition['origins'] = ['agent_autonomous']
    saved = teaching.save(definition)
    if change == 'capability':
        store.commit(saved | {'capability_hash': '0' * 64}, expected_revision=1, proposal_id='obsolete')
    if change == 'already_applied':
        teaching.run.steps = [{'tool': 'apply_training', 'params': {
            'unit_id': saved['unit_id'], 'teaching_id': saved['teaching_id'], 'revision': 1},
            'result': {'result': 'unknown'}}]
    if change == 'different_agent':
        teaching.run.agent_id = 'go2'
    assert select(teaching.run) is None


def test_ambiguous_or_observation_mapping_does_not_auto_apply(teaching, monkeypatch):
    saved = teaching.save()
    original = context.decision_context
    snapshot = original(teaching.run.agent_id)
    monkeypatch.setattr(context, 'decision_context', lambda *_a, **_k: snapshot)
    snapshot['records'].append(deepcopy(snapshot['records'][0]) | {'teaching_id': 'other'})
    assert select(teaching.run) is None
    snapshot['records'] = [snapshot['records'][0]]
    snapshot['records'][0]['definition']['trigger'] = {'event_id': 'made-up', 'where': {}}
    assert select(teaching.run) is None


@pytest.mark.parametrize('result', ['ok', 'unknown', 'blocked'])
def test_explicit_contextual_expression_prevents_second_generic_expression(teaching, result):
    teaching.save()
    specific = teaching.save(teaching.definition | {'situation': '被用户夸奖时'})
    teaching.run.steps = [{'tool': 'apply_training', 'params': {
        'unit_id': specific['unit_id'], 'teaching_id': specific['teaching_id'], 'revision': 1},
        'result': {'result': result}}]
    assert select(teaching.run) is None


def test_emotion_scope_does_not_change_ordinary_actions(teaching):
    saved = teaching.save()
    saved['loaded_revision'] = 1
    edge = next(edge for edge in teaching.catalog['transitions']
                if edge['action'] == 'SET_COLOR' and edge['src'] == 'OFF')
    params = {'r': 1, 'g': 2, 'b': 3, 'brightness': 40}
    request = {'transition_id': edge['id'], 'origin': 'user_delegated', 'params': params}
    facts = {'state_fresh': True, 'state': {'ism': 'OFF'}}
    result = policy.evaluate([saved], request, facts, now=time.time())
    assert result['params'] == params and result['teaching_refs'] == []
    execution = ExecutionContext('session', 'visitor', False, expires_at=time.time() + 60)
    result = service.evaluate_execution(teaching.card, {'ism': 'OFF'}, 'SET_COLOR', params,
                                       execution, [saved], lambda *_a: (True, None))
    assert result['allowed'] and result['params'] == params and result['training_refs'] == []


def test_emotion_conflicts_are_scoped_and_require_updating_existing_mapping(teaching):
    saved = teaching.save()
    with pytest.raises(service.TrainingError, match='training_conflict') as caught:
        teaching.save()
    assert caught.value.details[0]['code'] == 'emotion_overlap'
    other = teaching.save(teaching.definition | {'emotion': 'sad'})
    assert other['teaching_id'] != saved['teaching_id']
    assert policy.conflicts(Definition.model_validate(
        teaching.definition | {'emotion': None}).model_dump(), [saved]) == []


def test_emotion_needs_complete_parameters_and_is_not_an_observation(teaching):
    definition = deepcopy(teaching.definition)
    definition['steps'][0]['defaults'] = {}
    with pytest.raises(ValueError):
        policy.validate_definition(definition, teaching.catalog)
    with pytest.raises(ValueError, match='emotion_cannot_be_observation_trigger'):
        policy.validate_definition(teaching.definition | {
            'trigger': {'event_id': 'happy', 'where': {}}}, teaching.catalog)


@pytest.mark.parametrize('emotion', [None, 'sad'])
def test_proposal_retry_cannot_drop_or_change_emotion(teaching, emotion):
    original = {'operation': 'create', 'unit_id': teaching.card['unit_id'],
                'name': '开心用紫色', 'definition': teaching.definition}
    changed = deepcopy(original)
    changed['definition']['emotion'] = emotion
    with pytest.raises(ValueError, match='training_emotion_changed_on_retry'):
        _preserve_event_request(changed, [{'tool': 'propose_training', 'params': original}], teaching.catalog)


def test_retry_can_complete_missing_emotion_and_neutral_never_auto_applies(teaching):
    arguments = {'operation': 'create', 'unit_id': teaching.card['unit_id'],
                 'name': '开心用紫色', 'definition': teaching.definition}
    before = deepcopy(arguments)
    before['definition'].pop('emotion')
    _preserve_event_request(arguments, [{'tool': 'propose_training', 'params': before}], teaching.catalog)
    teaching.save(teaching.definition | {'emotion': 'neutral'})
    teaching.run.emotion = 'neutral'
    assert select(teaching.run) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('actor', ['agent', 'visitor'])
async def test_autonomous_read_uses_trusted_context_without_user_api(teaching, actor):
    saved = teaching.save()
    execution = ExecutionContext('autonomy', actor, False, origin_kind='agent_autonomous')
    async def forbidden(*_a, **_k):
        pytest.fail('autonomy must not impersonate a user API request')
    prepared = await prepare_training_application(select(teaching.run), execution, forbidden)
    assert prepared['training_ref'] == saved['teaching_id']
    assert prepared['training_revision'] == 1
    assert prepared['steps'][0]['defaults'] == teaching.definition['steps'][0]['defaults']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['dict', 'persist', 'actor', 'space', 'expired'])
async def test_autonomous_read_rejects_untrusted_context(teaching, change):
    teaching.save()
    execution = ExecutionContext('autonomy', 'agent', False, origin_kind='agent_autonomous')
    execution = {'dict': lambda: asdict(execution),
                 'persist': lambda: replace(execution, persist=True),
                 'actor': lambda: replace(execution, actor='admin'),
                 'space': lambda: replace(execution, space_id='other'),
                 'expired': lambda: replace(execution, expires_at=time.time() - 1)}[change]()
    with pytest.raises(ValueError, match='trusted_autonomous_context_required'):
        await prepare_training_application(select(teaching.run), execution)


@pytest.mark.asyncio
async def test_autonomous_preparation_rechecks_revision_and_origin(teaching):
    saved = teaching.save(teaching.definition | {'origins': ['user_delegated']})
    execution = ExecutionContext('autonomy', 'agent', False, origin_kind='agent_autonomous')
    args = select(teaching.run)
    with pytest.raises(ValueError, match='training_origin_mismatch'):
        await prepare_training_application(args, execution)
    store.commit(saved, expected_revision=1, proposal_id='revision-two')
    with pytest.raises(ValueError, match='training_revision_changed'):
        await prepare_training_application(args, execution)
