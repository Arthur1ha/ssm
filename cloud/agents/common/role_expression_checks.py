"""Emotion expressions enter the existing device teaching adapters once."""

import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.agents.common.training import context as training_context, tools
from cloud.esp32.capability_chain_checks import card as lamp_card
from cloud.space.execution import ExecutionContext


class HappyModel(JsonReplyModel):
    def __init__(self):
        self.calls = 0

    def bind_tools(self, declarations, **_kwargs):
        self.tools = {item['function']['name'] for item in declarations}
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        return AIMessage(content='{"kind":"final","text":"听到你这么说真开心。","emotion":"happy"}')


@pytest.fixture
def expression(monkeypatch, tmp_path):
    for key in ('SSM_CHECKPOINT_DIR', 'SSM_EFFECT_LEDGER_DIR', 'SSM_RUN_INDEX_DIR', 'SSM_TRAINING_DIR'):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setattr('cloud.api.intents.publish_agent_event', lambda *_a, **_k: {})
    monkeypatch.setattr('cloud.agents.common.progress.submit_progress', lambda *_a, **_k: None)
    monkeypatch.setattr('cloud.agents.common.runner.emit_agent_event', lambda *_a, **_k: None)
    selected, prepared, sent = [], [], []

    def select(run, **scope):
        selected.append((run.agent_id, run.emotion, scope))
        return {'unit_id': run.agent_id, 'teaching_id': 'happy_expression',
                'revision': 3, 'overrides': {}}

    async def prepare(args, context):
        prepared.append((args, context))
        return {'unit_id': args['unit_id'], 'training_ref': args['teaching_id']}

    async def behavior(data, context, **kwargs):
        sent.append((data, context, kwargs))
        return {'result': 'ok', 'training_ref': data['training_ref']}

    monkeypatch.setattr(training_context, 'emotion_training_call', select)
    monkeypatch.setattr(tools, 'prepare_training_application', prepare)
    return SimpleNamespace(selected=selected, prepared=prepared, sent=sent, behavior=behavior,
                           model=HappyModel(), tmp_path=tmp_path)


@pytest.mark.parametrize('origin', ['user_delegated', 'agent_autonomous'])
def test_lamp_emotion_uses_saved_workflow_and_run_replay_is_read_only(expression, monkeypatch, origin):
    from cloud.agents.lamp import graph
    from cloud.agents.lamp.workflows import runtime

    h = expression
    unit_id = lamp_card()['unit_id']
    agent = SimpleNamespace(_llm=h.model, mark_user_command=lambda _uid: None)
    monkeypatch.setattr(graph.cards, 'get_registry', lambda: SimpleNamespace(get_card=lambda _uid: lamp_card()))
    monkeypatch.setattr(graph, 'decision_messages', lambda run, **_: [HumanMessage(content=run.goal)])
    monkeypatch.setattr(graph, 'get_profile', lambda: {'prompt': '灯', 'name': '灯', 'traits': {}})
    monkeypatch.setattr(graph, 'submit_progress', lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, 'run_training_behavior', h.behavior)
    monkeypatch.setattr(graph, 'execute_lamp_tool', lambda *_a, **_k: pytest.fail('bypassed teaching workflow'))
    context = ExecutionContext('lamp-expression', 'agent' if origin == 'agent_autonomous' else 'admin',
                               False, origin_kind=origin, expires_at=int(time.time()) + 90)
    result = graph.run_lamp_loop(agent, '你真棒', unit_id, context, None, run_id='happy-lamp')
    replay = graph.run_lamp_loop(agent, '你真棒', unit_id, context, None, run_id='happy-lamp')
    assert result['result'] == replay['result'] == 'ok'
    assert result['emotion'] == 'happy' and result['reply'] == '听到你这么说真开心。'
    assert len(h.sent) == len(h.prepared) == h.model.calls == 1
    assert h.sent[0][1] is context
    assert h.sent[0][2]['run_id'] == 'happy-lamp:happy-lamp:emotion'
    assert h.selected == [(unit_id, 'happy', {'space_id': 'main', 'origin_kind': origin})]
    assert 'apply_training' in h.model.tools
    assert ('propose_training' in h.model.tools) == (origin == 'user_delegated')


def test_lamp_fixed_lighting_task_does_not_append_emotion(expression, monkeypatch):
    from cloud.agents.lamp import graph

    h = expression
    agent = SimpleNamespace(_llm=h.model, mark_user_command=lambda _uid: None)
    monkeypatch.setattr(graph.cards, 'get_registry', lambda: SimpleNamespace(get_card=lambda _uid: lamp_card()))
    monkeypatch.setattr(graph, 'decision_messages', lambda run, **_: [HumanMessage(content=run.goal)])
    monkeypatch.setattr(graph, 'get_profile', lambda: {'prompt': '灯', 'name': '灯', 'traits': {}})
    context = ExecutionContext('fixed-lighting', 'admin', False, expires_at=int(time.time()) + 90)
    result = graph.run_lamp_loop(agent, '亮度已经合适', lamp_card()['unit_id'], context,
                                 None, lighting_only=True, run_id='fixed-lamp')
    assert result['emotion'] == 'happy' and result['steps'] == []
    assert h.selected == h.prepared == h.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('restricted', [False, True])
@pytest.mark.parametrize('origin', ['user_delegated', 'agent_autonomous'])
async def test_go2_emotion_reuses_device_workflow_but_respects_restricted_tools(
        expression, monkeypatch, restricted, origin):
    from cloud.agents.go2 import graph
    from cloud.go2 import service
    from cloud.agents.go2.run_view import Go2RunView
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.agents.common.run_index import FileRunIndex
    from cloud.agents.go2.memory.episode import EpisodeMemory

    h = expression
    monkeypatch.setattr(graph, 'get_store', lambda: Go2RunView(
        checkpointer=FileCheckpointSaver(h.tmp_path / 'checkpoints'),
        index=FileRunIndex(h.tmp_path / 'index')))
    monkeypatch.setattr(graph, 'episode_memory', EpisodeMemory(h.tmp_path / 'episodes'))
    monkeypatch.setattr(graph, 'go2_messages', lambda run, **_: [HumanMessage(content=run.goal)])
    monkeypatch.setattr(graph, 'runtime_for', lambda _: None)
    if origin == 'agent_autonomous':
        from cloud.go2.mode.free_explore import drive
        runtime = SimpleNamespace(data={'epoch': 'current', 'autonomy_mode': 'free_explore'},
                                  active_reason=lambda: None, life_task_available=False)
        monkeypatch.setattr(graph, 'runtime_for', lambda _: runtime)
        monkeypatch.setattr(graph.go2, 'is_connected', True)
        monkeypatch.setattr(drive, '_generation', 8)
        monkeypatch.setattr(drive, 'owner_session_id', 'activity-owner')
        monkeypatch.setattr(drive, '_activity_id', lambda: 'current-activity')
        monkeypatch.setattr(drive, 'user_interrupt', False)
        monkeypatch.setattr(drive, '_pending_question_id', None)
    monkeypatch.setattr('cloud.agents.go2.personality.profile.get_profile', lambda: {
        'prompt': '狗', 'revision': 1, 'traits': {}})
    monkeypatch.setattr('cloud.agents.go2.personality.behavior.reply_persona', lambda _: '狗')
    monkeypatch.setattr(graph, 'get_text_llm', lambda: h.model)
    monkeypatch.setattr(service, 'run_training_behavior', h.behavior)
    monkeypatch.setattr(graph, 'execute_tool', lambda *_a, **_k: pytest.fail('bypassed teaching workflow'))
    context = ExecutionContext('go2-expression', 'agent' if origin == 'agent_autonomous' else 'admin',
                               False, origin_kind=origin, expires_at=int(time.time()) + 90)
    options = {'allowed_tools': frozenset({'go2_status'})} if restricted else {}
    result = await graph.run_go2_loop(context.session_id, '你真棒', context=context,
                                      run_id='happy-go2', **options)
    replay = await graph.run_go2_loop(context.session_id, '你真棒', context=context,
                                      run_id='happy-go2', **options)
    assert result['result'] == replay['result'] == 'ok'
    assert result['emotion'] == 'happy' and h.model.calls == 1
    if restricted:
        assert h.sent == h.prepared == h.selected == []
        assert result['tool_results'] == []
    else:
        assert len(h.sent) == len(h.prepared) == 1
        assert h.sent[0][1] is context
        assert h.sent[0][2]['run_id'] == 'happy-go2:happy-go2:emotion'
        assert result['tool_results'][0]['tool'] == 'apply_training'
        if origin == 'agent_autonomous':
            available = h.sent[0][2]['availability']
            assert available(context)
            drive._generation += 1
            assert not available(context)
            assert 'propose_training' not in h.model.tools
