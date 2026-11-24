"""Model-facing training queries exclude audit history without changing storage."""
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from cloud.agents.common.training import service, store, tools
from cloud.agents.common.training.catalog import catalog_for
from cloud.esp32.capability_chain_checks import card as lamp_card


@pytest.fixture
def teaching_api(monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path / 'training'))
    card = lamp_card()
    unit_id = card['unit_id']
    catalog = catalog_for(card)
    monkeypatch.setattr(service, '_targets', lambda space: {unit_id: card} if space == 'main' else {})
    monkeypatch.setattr(service, '_emit', lambda *_a, **_k: None)
    session = {'session_id': 'projection-session', 'space_id': 'main', 'actor': 'admin',
               'expires_at': time.time() + 300}
    session['training_source'] = service.UserText(session['session_id'], '只用于审计的原会话原话')
    definition = {'steps': [{'transition_ids': [edge['id'] for edge in catalog['transitions']
                        if edge['action'] == 'SET_COLOR'],
                    'defaults': {'r': 160, 'g': 50, 'b': 255, 'brightness': 20}}],
                  'origins': ['user_delegated'], 'situation': '准备阅读时'}
    requests = []

    def draft(name='阅读用紫色'):
        return service.propose({'operation': 'create', 'unit_id': unit_id, 'name': name,
            'effect_text': '准备阅读时使用紫色', 'definition': definition,
            'expected_revision': 0, 'capability_hash': catalog['hash']}, session)

    def save(name='阅读用紫色'):
        proposal = draft(name)
        return service.confirm(proposal['proposal_id'], 0, session)

    async def api(path, payload, context, *, method):
        requests.append((method, path, deepcopy(payload)))
        if method == 'GET':
            assert payload is None and path == '/api/training?unit_id=' + unit_id
            return service.snapshot(unit_id, session)
        assert method == 'POST' and path == '/api/training/proposals'
        return service.propose(payload, session)

    return SimpleNamespace(unit_id=unit_id, catalog=catalog, session=session, definition=definition,
                           draft=draft, save=save, api=api, requests=requests)


@pytest.mark.asyncio
async def test_all_removed_teachings_are_empty_current_query_without_purging_audit(teaching_api):
    env = teaching_api
    for index in range(6):
        record = env.save('旧事件' + str(index))
        if index < 2:
            record = store.commit(record | {'name': '已修改旧事件' + str(index)},
                expected_revision=record['revision'], proposal_id='edit-' + str(index))
        service.remove_record(record['teaching_id'], record['revision'], record['capability_hash'], env.session)
    management = service.snapshot(env.unit_id, env.session)
    assert len(management['records']) == 6
    assert all(row['desired_status'] == 'removed' for row in management['records'])
    assert sum(len(row['history']) for row in management['records']) == 8
    assert management['proposals'] == []
    stored = store.list_records(env.unit_id, space_id='main')

    result = await tools.list_training({'unit_id': env.unit_id}, {}, env.api)

    assert result['records'] == [] and result['proposals'] == []
    assert result['catalog'] == env.catalog
    assert 'edge_annotations' not in result
    assert [request[0] for request in env.requests] == ['GET']
    assert service.snapshot(env.unit_id, env.session) == management
    assert store.list_records(env.unit_id, space_id='main') == stored


@pytest.mark.asyncio
async def test_current_records_and_drafts_keep_definitions_identity_and_status_without_history(teaching_api):
    env = teaching_api
    record = env.save()
    disabled = service.set_enabled(record['teaching_id'], False, record['revision'], env.session)
    proposal = env.draft('新阅读事件')
    management = service.snapshot(env.unit_id, env.session)
    result = await tools.list_training({'unit_id': env.unit_id}, {}, env.api)
    row, = result['records']
    assert row['teaching_id'] == disabled['teaching_id'] and row['revision'] == disabled['revision']
    assert row['desired_status'] == 'disabled' and row['executable'] is False
    assert row['definition'] == disabled['definition']
    assert 'history' not in row and 'source_text' not in row
    draft, = result['proposals']
    for key in ('proposal_id', 'expected_revision', 'capability_hash'):
        assert draft[key] == proposal[key]
    assert draft['status'] == 'pending' and draft['confirmation_allowed'] is True
    assert draft['after']['definition'] == proposal['after']['definition']
    assert 'source_text' not in draft
    assert 'source_text' not in draft['after'] and 'history' not in draft['after']
    result['catalog']['transitions'].clear()
    row['definition']['steps'].clear()
    assert service.snapshot(env.unit_id, env.session) == management


@pytest.mark.asyncio
@pytest.mark.parametrize('flags,status', [
    ({'expired': True}, 'expired'), ({'cancelled': True}, 'cancelled'),
    ({'superseded_by': 'newer-draft'}, 'superseded'), ({'confirmed': True}, 'confirmed'),
    ({'confirmation_allowed': False, 'reasons': [{'code': 'revision_conflict'}]}, 'blocked'),
])
async def test_proposal_projection_marks_inactive_status_and_strips_nested_audit(teaching_api, flags, status):
    env = teaching_api
    proposal = env.draft()
    record = deepcopy(proposal['after'])
    record['history'] = [deepcopy(record)]
    proposal.update(before=record, **flags)
    source = {'catalog': env.catalog, 'records': [], 'proposals': [proposal]}
    original = deepcopy(source)

    async def api(*_a, **_k):
        return source

    result = await tools.list_training({'unit_id': env.unit_id}, {}, api)
    projected, = result['proposals']
    assert projected['status'] == status
    for key in ('proposal_id', 'expected_revision', 'capability_hash'):
        assert projected[key] == proposal[key]
    for key in ('before', 'after'):
        assert projected[key]['definition'] == proposal[key]['definition']
        assert 'history' not in projected[key] and 'source_text' not in projected[key]
    assert 'source_text' not in projected
    assert source == original


@pytest.mark.asyncio
async def test_proposing_after_deletion_creates_new_rule_and_returns_sanitized_draft(teaching_api):
    env = teaching_api
    old = env.save()
    service.remove_record(old['teaching_id'], old['revision'], old['capability_hash'], env.session)

    proposal = await tools.propose_training({'unit_id': env.unit_id, 'operation': 'create',
        'name': '新的阅读事件', 'definition': env.definition}, {}, env.api)

    assert proposal['status'] == 'pending' and proposal['operation'] == 'create'
    assert proposal['expected_revision'] == 0 and proposal['teaching_id'] != old['teaching_id']
    assert 'history' not in proposal['after'] and 'source_text' not in proposal['after']
    assert 'source_text' not in proposal
    post, = [request for request in env.requests if request[0] == 'POST']
    assert post[2]['operation'] == 'create' and 'teaching_id' not in post[2]
    stored, = store.list_records(env.unit_id, space_id='main')
    assert stored['desired_status'] == 'removed' and stored['history']


@pytest.mark.asyncio
async def test_draft_followup_preserves_exact_proposal_and_base_revision(teaching_api):
    env = teaching_api
    previous = env.draft()
    context = {'selection': {'unit_id': env.unit_id, 'proposal_id': previous['proposal_id']}}
    result = await tools.propose_training({'unit_id': env.unit_id, 'operation': 'create',
        'name': '新的阅读参数', 'definition': env.definition}, context, env.api)
    post, = [request for request in env.requests if request[0] == 'POST']
    assert post[2]['supersedes_proposal_id'] == previous['proposal_id']
    assert post[2]['expected_revision'] == previous['expected_revision'] == 0
    assert result['after']['definition'] == previous['after']['definition']
    assert result['teaching_id'] == previous['teaching_id']


@pytest.mark.asyncio
async def test_failed_training_query_propagates_instead_of_fabricating_empty_data(teaching_api):
    async def unavailable(*_a, **_k):
        raise tools.TrainingAPIError('training_storage_unavailable', status_code=503)

    with pytest.raises(tools.TrainingAPIError, match='training_storage_unavailable'):
        await tools.list_training({'unit_id': teaching_api.unit_id}, {}, unavailable)


@pytest.mark.asyncio
async def test_list_training_receipt_uses_existing_query_kind(teaching_api, monkeypatch):
    env = teaching_api
    monkeypatch.setattr(tools, 'internal_api_call', env.api)
    result = await tools.execute_training('list_training', {'unit_id': env.unit_id}, {})
    assert result['result'] == 'ok' and result['result_kind'] == 'query'
    assert result['records'] == [] and result['proposals'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['见到人要招招手，然后坐下', '看到人挥手持续60秒，然后坐下'])
async def test_model_cannot_add_repeat_interval_from_unrequested_or_action_duration(teaching_api, source):
    env = teaching_api
    definition = {**env.definition, 'cooldown_s': 60}
    with pytest.raises(ValueError, match='cooldown_not_requested'):
        await tools.propose_training({'unit_id': env.unit_id, 'operation': 'create',
            'name': '看到人打招呼', 'definition': definition}, {'source_text': source}, env.api)
    assert not [request for request in env.requests if request[0] == 'POST']
    assert store.list_records(env.unit_id, space_id='main') == []


@pytest.mark.asyncio
async def test_explicit_repeat_interval_is_saved_but_unrelated_update_cannot_change_it(teaching_api):
    env = teaching_api
    definition = {**env.definition, 'cooldown_s': 45}
    proposal = await tools.propose_training({'unit_id': env.unit_id, 'operation': 'create',
        'name': '阅读灯光', 'definition': definition}, {'source_text': '执行间隔用四十五秒'}, env.api)
    record = service.confirm(proposal['proposal_id'], 0, env.session)
    assert record['definition']['cooldown_s'] == 45
    request = {'unit_id': env.unit_id, 'operation': 'update', 'teaching_id': record['teaching_id'],
        'base_revision': 1, 'name': '新的阅读灯光', 'definition': {**definition, 'cooldown_s': 60}}
    with pytest.raises(ValueError, match='cooldown_not_requested'):
        await tools.propose_training(request, {'source_text': '把灯改成红色，闪烁60秒'}, env.api)
    request['definition']['cooldown_s'] = 45
    updated = await tools.propose_training(request, {'source_text': '把灯改成红色，闪烁60秒'}, env.api)
    assert updated['after']['definition']['cooldown_s'] == 45


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['不设执行间隔', '取消冷却时间', '把执行间隔去掉'])
async def test_user_can_remove_interval_from_selected_draft_without_resetting_other_fields(teaching_api, source):
    env = teaching_api
    env.definition['cooldown_s'] = 60
    previous = env.draft()
    context = {'source_text': source,
               'selection': {'unit_id': env.unit_id, 'proposal_id': previous['proposal_id']}}
    result = await tools.propose_training({'unit_id': env.unit_id, 'operation': 'create',
        'name': '不设间隔的阅读灯光', 'definition': {**env.definition, 'cooldown_s': None}}, context, env.api)
    assert result['after']['definition']['cooldown_s'] is None
    assert result['after']['definition']['steps'] == previous['after']['definition']['steps']
