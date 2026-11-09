"""Go2's prospective Graph entry preserves result and event contracts in isolation."""

import asyncio
import json
import time
from datetime import date

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.agents.go2 import graph as go2_graph
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.go2.run_view import Go2RunView, stable_reply_event_id
from cloud.agents.go2.memory.episode import EpisodeMemory, EventType, read_day
from cloud.space.execution import ExecutionContext


class ScriptedModel(JsonReplyModel):
    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []

    def bind_tools(self, tools, **kwargs):
        assert kwargs["parallel_tool_calls"] is False
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages)
        return next(self.replies)


@pytest.mark.asyncio
async def test_event_proposal_retry_preserves_trigger_and_can_repair_parameters(entry, monkeypatch):
    from copy import deepcopy
    from cloud.agents.common.training import tools
    definition = {'transition_ids': ['tr_sit'], 'origins': ['agent_autonomous'],
                  'conditions': [], 'cooldown_s': None, 'defaults': {},
                  'trigger': {'event_id': 'vision.owner_recognized',
                              'where': {}, 'params': {'recognized': True}}}
    original = {'operation': 'create', 'unit_id': 'go2', 'name': '看到主人就坐下',
                'effect_text': '确认主人后自动坐下', 'definition': definition}
    dropped = deepcopy(original)
    dropped['definition']['trigger'] = None
    fixed = deepcopy(original)
    fixed['definition']['trigger'] = {'event_id': 'vision.owner_recognized', 'where': {'recognized': True}, 'params': {}}
    replies = [AIMessage(content='', tool_calls=[{'name': 'propose_training',
        'id': 'attempt-' + str(index), 'args': args}]) for index, args in enumerate((original, dropped, fixed))]
    model = ScriptedModel([*replies, AIMessage(content='草案已准备，请确认。')])
    monkeypatch.setattr(go2_graph, 'get_text_llm', lambda: model)
    monkeypatch.setattr(go2_graph, 'runtime_for', lambda _: None)
    posts = []
    async def api(path, payload, context, *, method='POST'):
        if method == 'GET':
            return {'catalog': {'hash': 'a'*64}, 'records': []}
        posts.append(payload)
        if len(posts) == 1:
            raise tools.TrainingAPIError('invalid_definition', status_code=422,
                                         details={'details': 'recognized belongs in trigger.where'})
        return {'proposal_id': 'corrected-draft'}
    monkeypatch.setattr(tools, 'internal_api_call', api)
    context = ExecutionContext('retry-session', 'admin', True, expires_at=int(time.time())+60)
    result = await go2_graph.run_go2_loop('retry-session', '以后认出主人就坐下', context=context)
    assert len(posts) == 2, result
    assert posts[-1]['definition']['trigger'] == fixed['definition']['trigger']
    results = [step['result'] for step in result['tool_results']]
    assert results[0]['details']['details'] == 'recognized belongs in trigger.where'
    assert results[1]['error'].startswith('event_trigger_changed_on_retry')
    assert results[2]['result'] == 'ok'


@pytest.mark.asyncio
async def test_identity_prose_from_openai_compatible_provider_reaches_chat_reply(entry, monkeypatch):
    """Replay the live failure without the fake model's automatic JSON wrapping."""
    answer = "我是呆呆狗呀，就在这个空间里待着的机器狗。"

    class ProviderModel(ScriptedModel):
        def bind(self, **kwargs):
            self.response_options = kwargs
            return self

    model = ProviderModel([AIMessage(content=answer)])
    monkeypatch.setattr(go2_graph, 'get_text_llm', lambda: model)
    monkeypatch.setattr(go2_graph, 'runtime_for', lambda _: None)
    monkeypatch.setattr(go2_graph, 'execute_tool',
                        lambda *a, **k: pytest.fail('identity question dispatched an action'))
    context = ExecutionContext('identity-session', 'admin', False,
                               expires_at=int(time.time()) + 60)
    result = await go2_graph.run_go2_loop('identity-session', '你是谁呀', context=context)

    assert result['result'] == 'ok' and result['response'] == answer
    assert result['tool_results'] == [] and result['actions_taken'] == []
    assert len(model.inputs) == 1
    assert model.response_options == {'extra_body': {'response_format': {'type': 'json_object'}}}
    assert any(event == 'agent_reply' and fields.get('text') == answer
               for event, fields in entry)


@pytest.mark.asyncio
async def test_cancelled_entry_is_durable_and_new_run_does_not_replay(entry, monkeypatch):
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "go2_sport",
            "args": {"cmd": "Hello"}, "id": "interrupted-effect"}]),
        AIMessage(content="new conversation"),
    ])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph, "runtime_for", lambda _: None)
    monkeypatch.setattr(go2_graph.go2, "is_connected", True)
    entered = asyncio.Event()
    dispatched = []

    async def execute(name, params, *, context):
        dispatched.append(name)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("cancel-session", "admin", True,
                               expires_at=int(time.time() + 60))
    pending = asyncio.create_task(go2_graph.run_go2_loop(
        "cancel-session", "hello", context=context, run_id="cancel-old"))
    await asyncio.wait_for(entered.wait(), 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    stored = go2_graph.get_store().get("cancel-old", "cancel-session")
    assert stored.status == "cancelled"
    assert stored.steps[0]["result"]["result"] == "unknown"
    repeat = await go2_graph.run_go2_loop(
        "cancel-session", "hello", context=context, run_id="cancel-old")
    assert repeat["result"] == "cancelled"
    fresh = await go2_graph.run_go2_loop(
        "cancel-session", "new", context=context, run_id="cancel-new")
    assert fresh["result"] == "ok"
    assert dispatched == ["go2_sport"]
    assert go2_graph.get_store().get("cancel-old", "cancel-session").status == "cancelled"


@pytest.fixture
def entry(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    monkeypatch.setattr(go2_graph, "get_store", lambda: Go2RunView(
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        index=FileRunIndex(tmp_path / "index")))
    monkeypatch.setattr(go2_graph, "episode_memory", EpisodeMemory(tmp_path / "episodes"))
    monkeypatch.setattr(go2_graph, "go2_messages", lambda run, **kwargs: [
        HumanMessage(content=str(run.steps))])
    monkeypatch.setattr("cloud.agents.go2.personality.profile.get_profile",
                        lambda: {"prompt": "Go2", "revision": 1, "traits": {}})
    monkeypatch.setattr("cloud.agents.go2.personality.behavior.reply_persona",
                        lambda _profile: "Go2 persona")
    monkeypatch.setattr(go2_graph, "persona_reply_prompt", lambda *_a, **_k: "reply")
    monkeypatch.setattr("cloud.agents.common.progress.submit_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(go2_graph, "emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    published = []

    def publish(event_type, **fields):
        published.append((event_type, fields))
        return {"event_id": fields.get("event_id") or "evt-test"}

    monkeypatch.setattr("cloud.api.intents.publish_agent_event", publish)
    return published


@pytest.mark.asyncio
@pytest.mark.parametrize('pending', [True, False])
async def test_rule_revisions_use_existing_graph_and_preserve_rule_reference(entry, monkeypatch, pending):
    """An arbitrary follow-up revises its selected draft/rule through native tools."""
    from cloud.agents.common.training import tools as training_tools

    definition = {'transition_ids': ['tr_greeting'], 'origins': ['user_delegated'],
                  'conditions': [], 'cooldown_s': 45, 'defaults': {}, 'trigger': None}
    record = {'teaching_id': 'rule_greeting', 'revision': 4,
              'definition': {**definition, 'cooldown_s': 20}, 'name': '挥手后间隔20秒'}
    previous = {'proposal_id': 'draft_greeting', 'operation': 'create',
                'teaching_id': 'draft_rule', 'expected_revision': 0,
                'confirmed': False, 'after': {'definition': definition}}
    snapshot = {'catalog': {'hash': 'a' * 64, 'transitions': [
        {'id': 'tr_greeting', 'src': 'standing', 'dst': 'greeting'}]},
        'records': [] if pending else [record],
        'proposals': [previous] if pending else []}
    selection = {'unit_id': 'go2', 'transition_id': 'tr_greeting',
                 **({'proposal_id': 'draft_greeting'} if pending else {'teaching_id': 'rule_greeting'})}
    args = {'operation': 'create' if pending else 'update', 'unit_id': 'go2',
            'name': '挥手后间隔45秒', 'effect_text': '每次挥手结束后，隔45秒才会再次挥手。',
            'definition': definition}
    if not pending:
        args.update(teaching_id='rule_greeting', base_revision=4)
    model = ScriptedModel([
        AIMessage(content='', tool_calls=[{'name': 'list_training',
            'args': {'unit_id': 'go2'}, 'id': 'read-rule'}]),
        AIMessage(content='', tool_calls=[{'name': 'propose_training',
            'args': args, 'id': 'revise-rule'}]),
        AIMessage(content='按你补充的间隔改好了，请看卡片。'),
    ])
    monkeypatch.setattr(go2_graph, 'get_text_llm', lambda: model)
    monkeypatch.setattr(go2_graph, 'runtime_for', lambda _: None)
    calls = []

    async def api(path, payload, context, *, method='POST'):
        calls.append((method, path, payload, context))
        if method == 'GET':
            return snapshot
        return {'proposal_id': 'draft_revised', 'summaryText': payload['name'],
                'confirmed': False}

    async def no_device(*_args, **_kwargs):
        pytest.fail('editing a rule must not invoke a physical device tool')

    monkeypatch.setattr(training_tools, 'internal_api_call', api)
    monkeypatch.setattr(go2_graph, 'execute_tool', no_device)
    context = ExecutionContext('rule-session', 'admin', True, expires_at=int(time.time() + 60))
    result = await go2_graph.run_go2_loop('rule-session', '刚才那条间隔用四十五秒',
        context=context, run_id='rule-followup', selection=selection)
    assert result['result'] == 'ok'
    assert result['tool_results'][-1]['result']['proposal_id'] == 'draft_revised'
    writes = [call for call in calls if call[0] == 'POST']
    assert len(writes) == 1
    method, path, payload, trusted = writes[0]
    assert path == '/api/training/proposals'
    assert payload['name'] == '挥手后间隔45秒'
    assert payload['effect_text'] == args['effect_text']
    assert payload['definition']['cooldown_s'] == 45
    assert trusted['source_text'] == '刚才那条间隔用四十五秒'
    assert trusted['selection'] == selection
    if pending:
        assert payload['supersedes_proposal_id'] == 'draft_greeting'
        assert payload['operation'] == 'create'
        assert payload['expected_revision'] == 0
    else:
        assert payload['teaching_id'] == 'rule_greeting'
        assert payload['operation'] == 'update'
        assert payload['expected_revision'] == 4
    assert any(message.name == 'context'
               and json.loads(message.content.split('\n', 1)[1]).get('training_selection') == selection
               for message in model.inputs[0])
    assert 'confirm_training' not in {tool['function']['name'] for tool in model.bound_tools}


@pytest.mark.asyncio
async def test_verified_motion_result_and_reply_shape(entry, monkeypatch, tmp_path):
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "go2_sport",
                                              "args": {"cmd": "Hello"}, "id": "tool-1"}]),
        AIMessage(content="汪！招呼打完啦。"),
    ])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph.go2, "is_connected", True)
    dispatched = []

    async def execute(name, params, *, context):
        dispatched.append((name, params, context.actor))
        return {"result": "ok", "receipt": "webrtc"}

    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("go2-session", "admin", True, expires_at=int(time.time() + 60))
    streamed_steps = []
    result = await go2_graph.run_go2_loop("go2-session", "和我打招呼", context=context,
                                          run_id="go2-test-verified", on_step=streamed_steps.append)
    assert result["result"] == "ok"
    assert result["response"] == "汪！招呼打完啦。"
    assert len(model.inputs) == 2
    dialogues = read_day(date.today().isoformat(), go2_graph.episode_memory.episodes_dir)
    assert len(dialogues) == 1
    assert dialogues[0]["event_type"] == EventType.DIALOGUE.value
    assert "和我打招呼" in dialogues[0]["content"]
    assert result["actions_taken"] == ["go2_sport"]
    assert result["tool_results"][0]["result"]["receipt"] == "webrtc"
    assert dispatched == [("go2_sport", {"cmd": "Hello"}, "admin")]
    assert result["event_id"] == stable_reply_event_id(
        "go2-test-verified", session_id="go2-session")
    assert streamed_steps[0]["step_id"] == "tool-1"
    assert streamed_steps[0]["result"]["result"] == "ok"
    assert any(kind == "agent_progress" and fields.get("stage") == "observed"
               and fields["step_id"] == "tool-1" and fields["source"] == "device_chat"
               for kind, fields in entry)
    assert any(kind == "agent_reply" and fields["agent_id"] == "go2"
               and fields["reply_kind"] == "final" for kind, fields in entry)
    reopened = Go2RunView(checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
                          index=FileRunIndex(tmp_path / "index"))
    stored = reopened.get("go2-test-verified", "go2-session")
    assert stored is not None
    assert stored.response == result["response"]
    assert stored.reply_event_id == result["event_id"]
    repeated = await go2_graph.run_go2_loop("go2-session", "和我打招呼", context=context,
                                             run_id="go2-test-verified")
    assert repeated == result
    assert len(read_day(date.today().isoformat(), go2_graph.episode_memory.episodes_dir)) == 1
    assert dispatched == [("go2_sport", {"cmd": "Hello"}, "admin")]


@pytest.mark.asyncio
async def test_natural_find_runs_real_search_graph_without_scope_and_returns_evidence(entry, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from cloud.agents.go2.workflows import search
    from cloud.agents.go2.tests.test_search import Replay
    from cloud.go2 import connection, control, state as device_state
    from cloud.go2.navigation.navigator import navigator
    model = ScriptedModel([
        AIMessage(content='', tool_calls=[{'name': 'go2_find_object',
            'args': {'target': '白色猫头鹰'}, 'id': 'find-owl'}]),
        AIMessage(content='找到附近啦，我已经站稳了。'),
    ])
    monkeypatch.setattr(go2_graph, 'get_text_llm', lambda: model)
    monkeypatch.setattr(go2_graph, 'runtime_for', lambda _: None)
    monkeypatch.setattr(go2_graph.go2, 'is_connected', True)
    monkeypatch.setattr(device_state, 'runtime_for', lambda _: None)
    monkeypatch.setattr(connection, 'go2', SimpleNamespace(_odom_session_id='new-map'))
    monkeypatch.setattr(control, 'control_lease', control.ControlLease())
    monkeypatch.setattr(search, '_configured_scope', None)
    monkeypatch.delenv('GO2_SEARCH_BOUNDS', raising=False)
    monkeypatch.delenv('GO2_SEARCH_SESSION_ID', raising=False)
    monkeypatch.setattr(navigator, 'stop_and_confirm', AsyncMock(return_value={'result': 'ok'}))
    monkeypatch.setattr('cloud.go2.mode.free_explore.drive.resume_after_user', lambda: None)
    adapters = []
    class SearchDevice(Replay):
        def __init__(self, context, task_id, bounds, map_session):
            super().__init__([
                {'found': False, 'frame_id': 1},
                {'found': True, 'frame_id': 2, 'media_ref': '/owl-2',
                 'observation': {'proximity': 'nearby'}},
                {'found': True, 'frame_id': 3, 'media_ref': '/owl-3',
                 'observation': {'proximity': 'nearby'}},
            ], choices=[{'target': {'path_cost_m': .8}}])
            self.travelled_m = 0.
            self.context = context
            self.bounds = bounds
            adapters.append(self)
        async def record(self, observation):
            return {'observations': observation['frame_id'], 'unchecked_known_cells': 10}
    monkeypatch.setattr(search, 'DeviceSearch', SearchDevice)
    async def execute(name, params, *, context):
        assert name == 'go2_find_object'
        return await search.run_search(params['target'], context=context)
    monkeypatch.setattr(go2_graph, 'execute_tool', execute)
    now = time.time()
    context = ExecutionContext('natural-search', 'admin', False, expires_at=int(now + 600))
    result = await go2_graph.run_go2_loop('natural-search', '去找下室内的白色猫头鹰',
        context=context, run_id='owl-natural')
    assert result['result'] == 'ok'
    receipt = result['tool_results'][0]['result']
    assert receipt['outcome'] == 'nearby'
    assert receipt['evidence']['frame_id'] == 3
    assert receipt['scope'] == {'bounds': None, 'mode': 'reachable',
        'session_id': 'new-map', 'source': 'user_task'}
    assert result['response'] == '找到附近啦，我已经站稳了。'
    assert len(model.inputs) == 2
    assert adapters[0].context is context and adapters[0].bounds is None
    assert adapters[0].calls == ['observe', 'select', 'navigate', 'observe', 'observe', 'approach']
    stored = go2_graph.get_store().get('owl-natural', 'natural-search')
    assert stored.deadline >= now + search.SEARCH_SERVER_TIMEOUT_S
    assert search.configured_search_scope() is None
    repeated = await go2_graph.run_go2_loop('natural-search', '去找下室内的白色猫头鹰',
        context=context, run_id='owl-natural')
    assert repeated == result and len(adapters) == 1


@pytest.mark.asyncio
async def test_measured_move_reply_uses_persona_and_keeps_actual_distance(entry, monkeypatch):
    model = ScriptedModel([
        AIMessage(content='', tool_calls=[{'name': 'go2_move',
            'args': {'direction': 'forward', 'distance_m': 1.}, 'id': 'move-metre'}]),
        AIMessage(content='汪，往前走了约0.98米，已经停稳啦！'),
    ])
    monkeypatch.setattr(go2_graph, 'get_text_llm', lambda: model)
    monkeypatch.setattr(go2_graph.go2, 'is_connected', True)
    from cloud.agents.go2.prompts import persona_reply_prompt
    monkeypatch.setattr(go2_graph, 'persona_reply_prompt', persona_reply_prompt)
    monkeypatch.setattr('cloud.agents.go2.prompts.reply_persona', lambda profile: 'Go2 persona')
    monkeypatch.setattr('cloud.agents.go2.prompts.agent_names', lambda: {'go2': '测试狗'})
    message = '向前移动约0.98米，目标1米。已达到本次目标并确认停稳。'
    async def execute(*args, **kwargs):
        return {'result': 'ok', 'distance_m': .98, 'requested_distance_m': 1.,
                'stop': {'result': 'ok'}, 'message': message}
    monkeypatch.setattr(go2_graph, 'execute_tool', execute)
    context = ExecutionContext('move-reply', 'visitor', False, expires_at=time.time()+60)
    result = await go2_graph.run_go2_loop('move-reply', '前进1m', context=context, run_id='move-reply')
    assert result['response'] == '汪，往前走了约0.98米，已经停稳啦！'
    assert result['result'] == 'ok'
    assert len(model.inputs) == 2
    assert go2_graph.get_store().get('move-reply', 'move-reply').reply_source == 'decision_model'


@pytest.mark.asyncio
@pytest.mark.parametrize('persona_reply', [
    '我往前走了约1.76米，还没到你说的2米呢。后面迈不动了，我已经停稳啦。',
    '汪，目标是2米，只走了176厘米就走不动了，现在已经停下啦。',
])
async def test_partial_movement_keeps_receipt_and_natural_persona(entry, monkeypatch, persona_reply):
    model = ScriptedModel([
        AIMessage(content='', tool_calls=[{'name': 'go2_move',
            'args': {'direction': 'forward', 'distance_m': 2.}, 'id': 'move-partial'}]),
        AIMessage(content=persona_reply),
    ])
    monkeypatch.setattr(go2_graph, 'get_text_llm', lambda: model)
    monkeypatch.setattr(go2_graph.go2, 'is_connected', True)
    message = '向前移动约1.76米，目标2米。未完成本次目标，已确认停稳；后续未观察到继续移动。'
    async def execute(*args, **kwargs):
        return {'result': 'blocked', 'reason': 'motion_not_observed', 'distance_m': 1.761,
                'requested_distance_m': 2., 'stop': {'result': 'ok'}, 'message': message}
    monkeypatch.setattr(go2_graph, 'execute_tool', execute)
    context = ExecutionContext('partial-reply', 'visitor', False, expires_at=time.time()+60)
    result = await go2_graph.run_go2_loop('partial-reply', '前进两米', context=context, run_id='partial-reply')
    assert result['result'] == 'blocked'
    assert result['response'] == persona_reply
    assert result['tool_results'][0]['result']['distance_m'] == 1.761
    assert len(model.inputs) == 2
    assert go2_graph.get_store().get('partial-reply', 'partial-reply').reply_source == 'decision_model'


@pytest.mark.asyncio
async def test_unconfirmed_motion_stops_following_tool(entry, monkeypatch):
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "go2_sport",
                                              "args": {"cmd": "Hello"}, "id": "tool-2"}]),
        AIMessage(content="不应继续"),
    ])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    dispatched = []

    async def execute(name, params, *, context):
        dispatched.append(name)
        return {"result": "accepted"}

    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("go2-session-2", "admin", True, expires_at=int(time.time() + 60))
    result = await go2_graph.run_go2_loop("go2-session-2", "和我打招呼，然后跳舞", context=context,
                                          run_id="go2-test-accepted")
    assert result["result"] == "accepted"
    assert dispatched == ["go2_sport"]
    assert any(isinstance(message, HumanMessage) and message.content == "[]"
               for message in model.inputs[0])
    assert result["actions_taken"] == []


@pytest.mark.asyncio
async def test_visitor_cannot_dispatch_admin_tool_from_model(entry, monkeypatch):
    model = ScriptedModel([AIMessage(content="", tool_calls=[
        {"name": "go2_list_people", "args": {}, "id": "tool-forged"},
    ])])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    dispatched = []

    async def execute(name, params, *, context):
        dispatched.append(name)
        return {"result": "ok"}

    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("visitor-session", "visitor", False,
                               expires_at=int(time.time() + 60))
    result = await go2_graph.run_go2_loop("visitor-session", "列出人员", context=context,
                                          run_id="go2-test-visitor")
    assert result["result"] == "rejected"
    assert dispatched == []
    assert not read_day(date.today().isoformat(), go2_graph.episode_memory.episodes_dir)
    names = {tool["function"]["name"] for tool in model.bound_tools}
    assert "go2_list_people" not in names


@pytest.mark.asyncio
async def test_game_graph_exposes_only_allowed_tools_and_checks_live_guard(entry, monkeypatch):
    model = ScriptedModel([AIMessage(content="", tool_calls=[
        {"name": "go2_navigate_to_visible_target", "args": {"target": "红杯子"},
         "id": "game-visual"},
    ])])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    dispatched = []

    async def execute(name, params, *, context):
        dispatched.append(name)
        return {"result": "ok"}

    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("act_game", "visitor", False, "main",
                               "agent_autonomous", int(time.time() + 60))
    result = await go2_graph.run_go2_loop(
        "peer_game", "寻找同色物品", context=context,
        run_id="game-restricted-tools",
        allowed_tools=frozenset({"go2_observe", "go2_navigate_to_visible_target"}),
        tool_guard=lambda name, params: {"result": "blocked", "error": "observe_first"},
        task_context=lambda: "灯色 RGB=(255,0,0)", publish_reply=False)
    assert result["result"] == "blocked"
    assert dispatched == []
    assert {tool["function"]["name"] for tool in model.bound_tools} == {
        "go2_observe", "go2_navigate_to_visible_target"}
    assert any("灯色 RGB" in message.content for message in model.inputs[0])
    assert not any(kind == "agent_reply" for kind, _fields in entry)


@pytest.mark.asyncio
async def test_two_motion_calls_in_one_model_turn_execute_neither(entry, monkeypatch):
    model = ScriptedModel([AIMessage(content="", tool_calls=[
        {"name": "go2_sport", "args": {"cmd": "Hello"}, "id": "one"},
        {"name": "go2_sport", "args": {"cmd": "Dance1"}, "id": "two"},
    ])])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    dispatched = []

    async def execute(name, params, *, context):
        dispatched.append(name)
        return {"result": "ok"}

    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("parallel-session", "admin", True,
                               expires_at=int(time.time() + 60))
    result = await go2_graph.run_go2_loop("parallel-session", "打招呼并跳舞", context=context,
                                          run_id="go2-test-parallel")
    assert result["result"] == "blocked"
    assert result["actions_taken"] == []
    assert dispatched == []


@pytest.mark.asyncio
async def test_clarification_resumes_from_reopened_checkpoint(entry, monkeypatch, tmp_path):
    model = ScriptedModel([
        AIMessage(content='{"kind":"question","text":"去哪里？"}'),
        AIMessage(content="汪，去客厅呀！"),
    ])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph.go2, "is_connected", True)
    context = ExecutionContext("clarify-session", "admin", True,
                               expires_at=int(time.time() + 60))
    first = await go2_graph.run_go2_loop("clarify-session", "去那边", context=context,
                                         run_id="go2-test-clarify")
    assert first["result"] == "needs_clarification"
    assert first["question"] == "去哪里？"
    reopened = Go2RunView(checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
                          index=FileRunIndex(tmp_path / "index"))
    paused = reopened.get("go2-test-clarify", "clarify-session", require_head=True)
    assert paused is not None and paused.status == "needs_clarification"
    assert paused.reply_event_id == first["event_id"]
    second = await go2_graph.run_go2_loop("clarify-session", "另一个请求", context=context,
                                          run_id="go2-test-other-run")
    assert second["result"] == "rejected"
    assert second["error"] == "session_busy"
    resumed = await go2_graph.run_go2_loop("clarify-session", "客厅", context=context,
                                           run_id="go2-test-clarify", resume=True)
    assert resumed["result"] == "ok"
    assert resumed["response"] == "汪，去客厅呀！"
    stored = reopened.get("go2-test-clarify", "clarify-session")
    assert stored is not None and stored.response == resumed["response"]
    assert [fields["reply_kind"] for kind, fields in entry if kind == "agent_reply"] == [
        "clarification", "final"]
