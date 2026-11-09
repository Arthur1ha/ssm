"""Saved configuration references survive role and authenticated HTTP adapters."""

from types import SimpleNamespace
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.agents.common.runner import GraphRun
from cloud.agents.common.training import tools as training
from cloud.esp32.capability_chain_checks import card as lamp_card
from cloud.space.execution import ExecutionContext


PARAMS = {'r': 255, 'g': 236, 'b': 210, 'brightness': 180}
REFERENCE = {'training_ref': 'reading', 'training_revision': 3,
             'training_overrides': {'brightness': 180}}


@pytest.fixture
def context():
    return ExecutionContext('configuration-user', 'admin', True,
                            expires_at=int(time.time() + 120))


@pytest.fixture
def prepare(monkeypatch):
    calls = []

    async def prepared(arguments, context):
        calls.append((arguments, context))
        return {'unit_id': arguments['unit_id'], 'action': 'SET_COLOR',
                'params': dict(PARAMS), **REFERENCE}

    monkeypatch.setattr(training, 'prepare_training_application', prepared)
    return calls


@pytest.mark.asyncio
async def test_steward_applies_saved_parameters_and_trusted_reference(context, prepare, monkeypatch):
    from cloud.agents.steward import tools

    unit_id = lamp_card()['unit_id']
    monkeypatch.setattr(tools, '_cards', lambda _: {unit_id: lamp_card()})
    dispatched = []

    def invoke(trusted, arguments, step_id, deadline):
        dispatched.append((trusted, arguments, step_id))
        return {'result': 'ok'}

    monkeypatch.setattr(tools, '_invoke_skill', invoke)
    run = GraphRun('apply-run', context.session_id, '准备看会儿书', time.time() + 30)
    from dataclasses import asdict
    execute = tools.make_executor(run, asdict(context))
    result = await execute('apply_training', {
        'unit_id': unit_id, 'teaching_id': 'reading', 'revision': 3,
        'overrides': {'brightness': 180}}, 'apply-step')
    assert result['result'] == 'ok'
    trusted, arguments, step_id = dispatched[0]
    assert {key: trusted[key] for key in REFERENCE} == REFERENCE
    assert arguments['params'] == PARAMS
    assert arguments['skill_id'] == 'set_light_color'
    assert arguments['message'] == run.goal
    assert step_id == 'apply-step' and len(prepare) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,status', [('cancelled', 'cancelled'), ('expired', 'timeout'), ('activity', 'rejected')])
async def test_configuration_uses_existing_steward_admission(context, prepare, monkeypatch, reason, status):
    from dataclasses import asdict
    from cloud.agents.steward import tools

    trusted = asdict(context)
    run = GraphRun('admission', context.session_id, '使用教过的配置', time.time() + 30)
    if reason == 'cancelled':
        trusted['peer_request_id'] = 'cancelled-peer'
        monkeypatch.setattr(tools, 'peer_coordination_cancelled', lambda _: True)
    elif reason == 'expired':
        trusted['expires_at'] = time.time() - 1
    else:
        run.steps = [{'tool': 'start_activity', 'result': {'result': 'ok'}}]
    result = await tools.make_executor(run, trusted)('apply_training', {
        'unit_id': lamp_card()['unit_id'], 'teaching_id': 'reading', 'revision': 3}, 'not-dispatched')
    assert result['result'] == status and prepare == []


def test_steward_mqtt_boundary_keeps_reference(context, monkeypatch):
    from cloud.agents.steward import tools
    from cloud.esp32 import tool_executor
    from dataclasses import asdict

    card = {**lamp_card(), 'unit_id': 'other_led'}
    monkeypatch.setattr(tools, '_cards', lambda _: {'other_led': card})
    monkeypatch.setattr(tools, 'validate_public_skill', lambda *_: {'kind': 'mqtt'})
    monkeypatch.setattr(tools, 'allows_step', lambda *_: True)
    seen = []

    def execute(**arguments):
        seen.append(arguments)
        return {'result': 'ok'}

    monkeypatch.setattr(tool_executor, 'execute_physical', execute)
    result = tools._invoke_skill({**asdict(context), **REFERENCE}, {
        'unit_id': 'other_led', 'skill_id': 'set_light_color', 'params': PARAMS,
    }, 'mqtt-apply', time.time() + 30)
    assert result['result'] == 'ok'
    assert seen[0]['params'] == PARAMS
    assert seen[0]['context'].training_ref == 'reading'
    assert seen[0]['context'].training_revision == 3
    assert seen[0]['context'].training_overrides == {'brightness': 180}


def test_http_reference_reaches_authenticated_go2_context(context, monkeypatch):
    from cloud.agents.steward import tools
    from cloud.go2.card import _build_go2_card
    from cloud.go2.router import ChatRequest, _training_context
    from dataclasses import asdict

    monkeypatch.setattr(tools, '_cards', lambda _: {'go2': _build_go2_card()})
    monkeypatch.setattr(tools, 'validate_public_skill', lambda *_: {'kind': 'http', 'endpoint': '/api/go2/chat'})
    monkeypatch.setattr(tools, 'allows_step', lambda *_: True)
    bodies = []

    def dispatch(endpoint, body, timeout):
        bodies.append(body)
        return {'result': 'ok'}

    monkeypatch.setattr(tools.transport, 'do_http_dispatch', dispatch)
    reference = {**REFERENCE, 'training_ref': 'greeting', 'training_overrides': {}}
    tools._invoke_skill({**asdict(context), **reference}, {
        'unit_id': 'go2', 'skill_id': 'go2_sport', 'params': {'cmd': 'Hello'},
    }, 'go2-apply', time.time() + 30)
    req = ChatRequest.model_validate(bodies[0])
    execution = _training_context(req, context)
    assert execution.training_ref == 'greeting'
    assert execution.training_revision == 3 and execution.training_overrides == {}
    assert execution.session_id == context.session_id
    assert _training_context(ChatRequest(message='你好'), context) is context
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _training_context(ChatRequest(message='你好', training={
            'unit_id': 'other_led', 'teaching_id': 'reading', 'revision': 3}), context)


@pytest.mark.asyncio
async def test_go2_stream_keeps_selected_skill_parameters_and_reference(context, monkeypatch):
    from cloud.go2 import router
    from cloud.agents.go2 import graph

    received = []

    async def run(session_id, message, **kwargs):
        received.append((session_id, message, kwargs))
        return {'result': 'ok', 'response': '打过招呼了。'}

    monkeypatch.setattr(graph, 'run_go2_loop', run)
    request = router.ChatRequest(message='按之前教过的方式打招呼', skill_id='go2_sport',
        params={'cmd': 'Hello'}, training={'unit_id': 'go2', 'teaching_id': 'greeting', 'revision': 3})
    response = await router.go2_chat_stream(request, context)
    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks and len(received) == 1
    kwargs = received[0][2]
    assert kwargs['skill_id'] == 'go2_sport' and kwargs['params'] == {'cmd': 'Hello'}
    assert kwargs['context'].training_ref == 'greeting'
    assert kwargs['context'].training_revision == 3


class ApplyModel(JsonReplyModel):
    def __init__(self, unit_id):
        self.unit_id, self.calls = unit_id, 0

    def bind_tools(self, *_args, **_kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content='', tool_calls=[{
                'name': 'apply_training', 'id': 'saved-config', 'args': {
                    'unit_id': self.unit_id, 'teaching_id': 'reading', 'revision': 3,
                    'overrides': {'brightness': 180}}}])
        return AIMessage(content='已经按你教的配置调好了。')


def test_lamp_graph_dispatches_saved_configuration_without_rewrite(
        context, prepare, monkeypatch, tmp_path):
    from cloud.agents.lamp import graph

    for key in ('SSM_CHECKPOINT_DIR', 'SSM_EFFECT_LEDGER_DIR', 'SSM_RUN_INDEX_DIR'):
        monkeypatch.setenv(key, str(tmp_path / key))
    unit_id = lamp_card()['unit_id']
    model = ApplyModel(unit_id)
    agent = SimpleNamespace(_llm=model, mark_user_command=lambda _unit: None)
    monkeypatch.setattr(graph.cards, 'get_registry', lambda: SimpleNamespace(get_card=lambda _uid: lamp_card()))
    monkeypatch.setattr(graph, 'decision_messages', lambda run, **_: [HumanMessage(content=run.goal)])
    monkeypatch.setattr(graph, 'get_profile', lambda: {'prompt': '灯', 'name': '灯', 'traits': {}})
    monkeypatch.setattr(graph, 'submit_progress', lambda *_a, **_k: None)
    monkeypatch.setattr('cloud.api.intents.publish_agent_event', lambda *_a, **_k: {})
    sent = []

    def execute(name, params, step_id, **kwargs):
        sent.append((name, params, kwargs['context']))
        return {'result': 'ok', 'task_id': 'physical-configuration'}

    monkeypatch.setattr(graph, 'execute_lamp_tool', execute)
    result = graph.run_lamp_loop(agent, '准备看会儿书', unit_id, context,
                                 lambda *_a, **_k: None, run_id='lamp-apply')
    assert result['result'] == 'ok' and '调好了' in result['reply']
    assert model.calls == 2  # One tool decision, one final answer; no rewrite.
    assert sent[0][0] == 'set_led_color' and sent[0][1] == PARAMS
    assert sent[0][2].training_ref == 'reading'
    assert sent[0][2].training_revision == 3
    replay = graph.run_lamp_loop(agent, '准备看会儿书', unit_id, context,
                                 lambda *_a, **_k: None, run_id='lamp-apply')
    assert replay['result'] == 'ok' and len(sent) == 1 and model.calls == 2


@pytest.mark.asyncio
async def test_go2_graph_maps_saved_action_to_existing_tool(context, prepare, monkeypatch, tmp_path):
    from cloud.agents.go2 import graph
    from cloud.agents.go2.run_view import Go2RunView
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.agents.common.run_index import FileRunIndex
    from cloud.agents.go2.memory.episode import EpisodeMemory

    async def prepared(arguments, trusted):
        return {'unit_id': 'go2', 'action': 'Hello', 'params': {},
                **{**REFERENCE, 'training_ref': 'greeting', 'training_overrides': {}}}

    monkeypatch.setattr(training, 'prepare_training_application', prepared)
    monkeypatch.setenv('SSM_CHECKPOINT_DIR', str(tmp_path / 'checkpoints'))
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    monkeypatch.setattr(graph, 'get_store', lambda: Go2RunView(
        checkpointer=FileCheckpointSaver(tmp_path / 'checkpoints'), index=FileRunIndex(tmp_path / 'index')))
    monkeypatch.setattr(graph, 'episode_memory', EpisodeMemory(tmp_path / 'episodes'))
    monkeypatch.setattr(graph, 'go2_messages', lambda run, **_: [HumanMessage(content=run.goal)])
    monkeypatch.setattr(graph, 'runtime_for', lambda _: None)
    monkeypatch.setattr(graph.go2, 'is_connected', True)
    monkeypatch.setattr('cloud.agents.go2.personality.profile.get_profile', lambda: {
        'prompt': '狗', 'revision': 1, 'traits': {'extraversion': 50}})
    monkeypatch.setattr('cloud.agents.common.progress.submit_progress', lambda *_a, **_k: None)
    monkeypatch.setattr('cloud.api.intents.publish_agent_event', lambda *_a, **_k: {})
    model = ApplyModel('go2')
    monkeypatch.setattr(graph, 'get_text_llm', lambda: model)
    sent = []

    async def execute(name, params, *, context):
        sent.append((name, params, context))
        return {'result': 'ok'}

    monkeypatch.setattr(graph, 'execute_tool', execute)
    result = await graph.run_go2_loop(context.session_id, '按之前教你的打招呼',
                                      context=context, run_id='go2-apply')
    assert result['result'] == 'ok'
    assert sent[0][0:2] == ('go2_sport', {'cmd': 'Hello'})
    assert sent[0][2].training_ref == 'greeting'
    assert sent[0][2].training_revision == 3 and model.calls == 2
    replay = await graph.run_go2_loop(context.session_id, '按之前教你的打招呼',
                                      context=context, run_id='go2-apply')
    assert replay['result'] == 'ok' and len(sent) == 1 and model.calls == 2
