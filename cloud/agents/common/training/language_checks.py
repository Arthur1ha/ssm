import pytest

from cloud.agents.common.training.tools import propose_training, list_training


@pytest.mark.asyncio
async def test_preview_only_uses_snapshot_and_trusted_source():
    calls = []
    async def api(path, payload, context, *, method='POST'):
        calls.append((method, path, payload, context))
        if method == 'GET':
            return {'catalog': {'hash': 'a' * 64}, 'records': [{'teaching_id': 't1', 'revision': 3, 'name': '招呼'}]}
        return {'proposal_id': 'p1'}
    context = {'source_text': '  改成半分钟  ', 'session_id': 's1', 'run_id': 'r1'}
    result = await propose_training({'operation': 'remove', 'teaching_id': 't1', 'base_revision': 3, 'unit_id': 'go2'}, context, api)
    assert result['proposal_id'] == 'p1'
    assert [c[0] for c in calls] == ['GET', 'POST']
    assert calls[1][2] == {'operation': 'remove', 'unit_id': 'go2', 'teaching_id': 't1', 'expected_revision': 3, 'capability_hash': 'a' * 64}
    assert calls[1][3]['source_text'] == '  改成半分钟  '


@pytest.mark.asyncio
async def test_selection_and_source_cannot_be_forged():
    async def api(path, payload, context, *, method='POST'):
        return {'catalog': {'hash': 'a' * 64}, 'records': []}
    with pytest.raises(ValueError):
        await propose_training({'operation': 'remove', 'unit_id': 'go2', 'source_text': 'forged'}, {}, api)
    with pytest.raises(ValueError):
        await list_training({'unit_id': 'go2'}, {'selection': {'unit_id': 'lamp', 'transition_id': 'tr_1'}}, api)


def test_role_schemas_expose_shared_training_only():
    from cloud.agents.steward.tools import STEWARD_TOOL_SCHEMAS
    from cloud.agents.lamp.tools.schemas import visible_schemas
    from cloud.agents.go2.tools.declarations import tool_schemas
    from cloud.agents.common.training.tools import SCHEMAS
    assert 'save_rule' not in STEWARD_TOOL_SCHEMAS
    for schemas in (STEWARD_TOOL_SCHEMAS, visible_schemas(lighting_only=False), {**tool_schemas(), **SCHEMAS}):
        assert set(SCHEMAS) <= set(schemas)
        assert 'confirm_training' not in schemas
        assert 'source_text' not in schemas['propose_training'].model_fields


def test_selection_validation(monkeypatch):
    from fastapi import HTTPException
    from cloud.api.intents import validate_training_selection
    from cloud.agents.common.training import service
    monkeypatch.setattr(service, 'snapshot', lambda unit, session: {
        'catalog': {'transitions': [{'id': 'tr_1', 'trainable': True}]},
        'records': [{'teaching_id': 't1', 'definition': {'transition_ids': ['tr_1']}}]})
    session = {'session_id': 's', 'actor': 'admin', 'expires_at': 9999999999}
    assert validate_training_selection({'unit_id': 'go2', 'transition_id': 'tr_1', 'teaching_id': 't1'}, session)['teaching_id'] == 't1'
    for selected in ({'unit_id': 'go2', 'transition_id': 'bad'},
                     {'unit_id': 'go2', 'transition_id': 'tr_1', 'teaching_id': 'other'},
                     {'unit_id': 'go2', 'transition_id': 'tr_1', 'extra': 1}):
        with pytest.raises(HTTPException):
            validate_training_selection(selected, session)


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['steward', 'lamp', 'go2'])
async def test_scripted_model_uses_role_schemas_and_proposal_only(role, tmp_path, monkeypatch):
    import time
    from langchain_core.messages import AIMessage, HumanMessage
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.agents.common.runner import GraphRun, run_role_graph
    from cloud.agents.common.effects import FileEffectLedger
    from cloud.agents.common.training.tools import SCHEMAS
    from cloud.agents.steward.tools import STEWARD_TOOL_SCHEMAS
    from cloud.agents.lamp.tools.schemas import visible_schemas
    from cloud.agents.go2.tools.declarations import tool_schemas
    declarations = {'steward': STEWARD_TOOL_SCHEMAS,
                    'lamp': visible_schemas(lighting_only=False),
                    'go2': {**tool_schemas(), **SCHEMAS}}[role]
    class ScriptedModel:
        def __init__(self): self.calls = 0
        def bind_tools(self, tools, **kwargs):
            self.names = {t['function']['name'] for t in tools}
            return self
        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content='', tool_calls=[{'name': 'propose_training', 'id': 'call1',
                    'args': {'operation': 'remove', 'unit_id': 'go2', 'teaching_id': 't1', 'base_revision': 3}}])
            return AIMessage(content='草案已准备，请确认。')
    model = ScriptedModel()
    calls = []
    async def api(path, payload, context, *, method='POST'):
        calls.append((method, path))
        if method == 'GET':
            return {'catalog': {'hash': 'a' * 64}, 'records': [{'teaching_id': 't1', 'revision': 3, 'name': '招呼'}]}
        return {'proposal_id': 'p1'}
    async def execute(name, arguments, step_id):
        return {'result': 'ok', **await propose_training(arguments, {'source_text': '撤销', 'session_id': 's', 'run_id': 'r'}, api)}
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    monkeypatch.setenv('SSM_CHECKPOINT_DIR', str(tmp_path / 'checkpoints'))
    run = GraphRun('r', 's', '撤销教导', time.time() + 30, agent_id=role)
    await run_role_graph(run, model=model, tool_schemas=declarations,
                         build_messages=lambda _: [HumanMessage(content='撤销教导')], execute=execute,
                         checkpointer=FileCheckpointSaver(), effect_ledger=FileEffectLedger(),
                         effectful=lambda name: name == 'propose_training')
    assert run.steps[0]['result']['proposal_id'] == 'p1'
    assert calls == [('GET', '/api/training?unit_id=go2'), ('POST', '/api/training/proposals')]
    assert 'confirm_training' not in model.names


@pytest.mark.asyncio
async def test_create_derives_name_and_rejects_wrong_selection():
    calls = []
    async def api(path, payload, context, *, method='POST'):
        calls.append((method, payload))
        if method == 'GET':
            return {'catalog': {'hash': 'a' * 64, 'transitions': [{'id': 'tr_1', 'src': 'standing', 'dst': 'greeting'}]}, 'records': []}
        return {'proposal_id': 'p1'}
    definition = {'transition_ids': ['tr_1'], 'origins': ['agent_autonomous'], 'conditions': [],
                  'cooldown_s': None, 'defaults': {}, 'trigger': None}
    context = {'selection': {'unit_id': 'go2', 'transition_id': 'tr_1'}, 'source_text': '以后这样'}
    await propose_training({'operation': 'create', 'unit_id': 'go2', 'definition': definition}, context, api)
    assert calls[-1][1]['name'] == 'standing → greeting'
    assert calls[-1][1]['expected_revision'] == 0
    assert calls[-1][1]['definition']['cooldown_s'] is None
    assert calls[-1][1]['definition']['trigger'] is None
    # Runner's exclude_none removes required nullable nested fields.
    await propose_training({'operation': 'create', 'unit_id': 'go2', 'definition': {k: v for k, v in definition.items() if v is not None}}, context, api)
    assert calls[-1][1]['definition']['trigger'] is None
    with pytest.raises(ValueError):
        await propose_training({'operation': 'create', 'unit_id': 'go2', 'definition': definition},
                               {**context, 'selection': {'unit_id': 'go2', 'transition_id': 'tr_2'}}, api)


@pytest.mark.asyncio
async def test_internal_adapter_serializes_only_trusted_source(monkeypatch):
    import base64, json, time
    from cloud.agents.common.training import tools
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return b'{"proposal_id":"p1"}'
    class Opener:
        def open(self, request, timeout):
            captured.update(dict(request.header_items()))
            return Response()
    monkeypatch.setenv('SSM_INTERNAL_SERVICE_TOKEN', 'unit-test-token-only')
    monkeypatch.setattr(tools.urllib.request, 'build_opener', lambda *_: Opener())
    async def direct(call): return call()
    monkeypatch.setattr(tools.asyncio, 'to_thread', direct)
    context = {'session_id': 's', 'run_id': 'r', 'source_text': '  补充原话\n',
               'actor': 'admin', 'persist': True, 'space_id': 'main',
               'origin_kind': 'user_delegated', 'expires_at': time.time() + 60}
    await tools.internal_api_call('/api/training/proposals', {'unit_id': 'go2'}, context)
    source = json.loads(base64.b64decode(captured['X-ssm-training-source']))
    assert source == {'session_id': 's', 'run_id': 'r', 'text': '  补充原话\n'}
    visitor = {**context, 'actor': 'visitor', 'persist': False}
    captured.clear()
    await tools.internal_api_call('/api/training?unit_id=go2', None, visitor, method='GET')
    assert 'X-ssm-training-source' not in captured
    assert captured['X-ssm-persist'] == 'false'
    with pytest.raises(ValueError):
        await tools.internal_api_call('/api/training/proposals', {}, visitor)


@pytest.mark.asyncio
async def test_life_request_id_forwarded_through_gateway_and_guarded_router(monkeypatch):
    from cloud.api import intents
    from cloud.go2 import router
    from cloud.go2.tools import executor
    from cloud.space.execution import ExecutionContext
    seen = []
    async def request(path, method, session, body):
        seen.append((path, body))
        return {'result': 'ok'}
    async def execute(name, params, *, context, request_id=None):
        seen.append((name, params, request_id))
        return {'result': 'ok'}
    monkeypatch.setattr(intents, '_internal_request', request)
    monkeypatch.setattr(intents, 'ensure_go2_action', lambda _: None)
    monkeypatch.setattr(executor, 'execute_tool', execute)
    session = {'session_id': 's', 'actor': 'admin', 'expires_at': 9999999999}
    await intents._dispatch_go2('life', {'action': 'wake'}, session, request_id='req_1')
    assert seen.pop() == ('/api/go2/life', {'action': 'wake', 'request_id': 'req_1'})
    await router.go2_life(router.LifeRequest(action='wake', request_id='req_1'),
                          ExecutionContext('s', 'admin', True, expires_at=9999999999))
    assert seen.pop() == ('go2_life', {'action': 'wake'}, 'req_1')
    with pytest.raises(ValueError):
        router.LifeRequest(action='wake', request_id='../invalid')


def test_signed_resume_preserves_original_text_and_selection():
    from cloud.mqtt_signing import sign_message
    from cloud.orchestrator.trusted_context import parse_trusted_intent
    now = 1800000000
    secret = 'unit-test-signing-secret'
    selection = {'unit_id': 'go2', 'transition_id': 'tr_1', 'teaching_id': 't1'}
    payload = {'text': '  改成半分钟\n', 'resume_run_id': 'r1', 'selection': selection,
               'context': {'session_id': 's1', 'actor': 'admin', 'persist': True, 'space_id': 'main',
                           'issued_at': now - 10, 'expires_at': now + 60}}
    signed = sign_message('ssm/intent/s1', payload, signing_secret=secret, now=now)
    context = parse_trusted_intent(signed, 's1', signing_secret=secret, now=now)
    assert context['user_msg'] == payload['text']
    assert context['selection'] == selection
    assert context['resume_run_id'] == 'r1'
