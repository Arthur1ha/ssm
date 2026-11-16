"""New activity decisions use their final text without a second reply model."""
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from cloud.agents.lamp.workflows.reactive import run_reactive_event


@pytest.fixture
def activity_reply(monkeypatch, tmp_path):
    # Import consumers before replacing the source getter so lazy imports do
    # not retain a function closing over this fixture after teardown.
    from cloud.api import activity as activity_api

    for key in ('SSM_TRAINING_DIR', 'SSM_CHECKPOINT_DIR', 'SSM_EFFECT_LEDGER_DIR'):
        monkeypatch.setenv(key, str(tmp_path / key))
    activity_id = 'act_request'
    activity = {'activity_id': activity_id, 'status': 'active', 'devices': {
        'esp32_desk_led': {'status': 'joined', 'owner': activity_id}}}
    state = SimpleNamespace(activity=activity, published=[], calls=0, dispatched=[])
    profile = {'agent_id': 'esp32_desk_led', 'name': '灯', 'prompt': '直接接话。',
               'traits': {'social_initiative': 70, 'adjustment_sensitivity': 50}}
    monkeypatch.setattr('cloud.agents.lamp.personality.profile.get_profile', lambda: profile)
    monkeypatch.setattr('cloud.personality.names.agent_names', lambda: {})
    monkeypatch.setattr('cloud.space.context.get_space_context', lambda *_args, **_kwargs: {
        'peers': [], 'recent_discoveries': [], 'activity': state.activity})
    activity_store = SimpleNamespace(current=lambda: state.activity)
    monkeypatch.setattr('cloud.space.activity.get_activity_store', lambda: activity_store)
    monkeypatch.setattr(activity_api, 'get_activity_store', lambda: activity_store)
    agent = SimpleNamespace(_state=None, _autonomy_epoch=1, _belief_summary='',
        _belief_history=[], _beliefs_since_summary=0, _peer_game_id=None,
        _should_act=lambda: True, _device_online=lambda: True,
        _find_led_unit=lambda: 'esp32_desk_led')
    state.agent = agent

    def run(response, *, after_model=lambda: None, opportunity_kind='activity_request'):
        class Model:
            def bind_tools(self, *_args, **_kwargs):
                return self

            def invoke(self, _messages):
                state.calls += 1
                after_model()
                return response

        return run_reactive_event(agent, {
            'lighting_eligible': False,
            'game_readiness': {'go2': {'ready': False, 'reason': 'lamp_result_unconfirmed'}},
            'social_opportunity': {'kind': opportunity_kind, 'activity_id': activity_id,
                                   'event_ids': ['activity_request:' + activity_id]}},
            model=Model(), checkpointer=InMemorySaver(),
            publish=lambda kind, **facts: state.published.append((kind, facts)))

    state.run = run
    return state


def test_busy_lamp_activity_final_text_reaches_existing_space_reply(activity_reply):
    h = activity_reply
    answer = '先等上一条灯光指令确认，再玩。'
    result = h.run(AIMessage(content=answer))
    assert result['reply'] == answer
    assert h.calls == 1
    assert h.published == [('agent_reply', {
        'agent_id': 'esp32_desk_led', 'scope': 'space', 'space_id': 'main',
        'source': 'autonomous', 'status': 'ok', 'run_id': result['run_id'], 'text': answer,
        'emotion': 'neutral'})]


@pytest.mark.parametrize('emotion,expected', [('curious', 'curious'), (None, 'neutral'),
                                             ({'name': 'happy'}, 'neutral'), ('invented', 'neutral')])
def test_activity_reply_emotion_uses_same_model_call_and_never_rejects_text(activity_reply, emotion, expected):
    import json

    h = activity_reply
    answer = '我想先观察一下。'
    result = h.run(AIMessage(content=json.dumps({'text': answer, 'emotion': emotion})))
    assert result['reply'] == answer and result['status'] == 'ok'
    assert result['steps'] == [] and h.calls == 1
    assert len(h.published) == 1
    assert h.published[0][1]['emotion'] == expected


def test_explicit_peer_message_does_not_add_another_activity_reply(activity_reply):
    h = activity_reply

    def dispatch(*args, **_kwargs):
        h.dispatched.append(args)
        h.agent._last_reactive_tool_result = {'result': 'accepted', 'message_id': 'sent'}
        return True

    h.agent._dispatch_tool = dispatch
    result = h.run(AIMessage(content='', tool_calls=[{
        'name': 'send_message', 'id': 'send_once',
        'args': {'target': 'go2', 'content': '等我上一条灯光指令确认，再玩。'}}]))
    assert len(h.dispatched) == 1 and result['steps'][0]['tool'] == 'send_message'
    assert h.calls == 1
    assert h.published == []


@pytest.mark.parametrize('change', ['activity', 'epoch', 'mode', 'online'])
def test_old_or_invalidated_activity_decision_cannot_publish(activity_reply, change):
    h = activity_reply

    def changed():
        if change == 'activity':
            h.activity = {**h.activity, 'activity_id': 'act_new'}
        elif change == 'epoch':
            h.agent._autonomy_epoch += 1
        elif change == 'mode':
            h.agent._should_act = lambda: False
        else:
            h.agent._device_online = lambda: False

    h.run(AIMessage(content='先等等。'), after_model=changed)
    assert h.published == []
    assert h.calls == 1


def test_regular_sensor_decision_keeps_its_existing_silence(activity_reply):
    h = activity_reply
    h.run(AIMessage(content='目前不用调灯。'), opportunity_kind='idle')
    assert h.published == []
    assert h.calls == 1


@pytest.mark.parametrize('status', ['ok', 'blocked', 'unknown'])
def test_activity_emotion_uses_confirmed_behavior_and_preserves_receipt(
        activity_reply, monkeypatch, status):
    from cloud.agents.common.training import context as training_context, tools
    from cloud.agents.lamp.workflows import runtime

    h = activity_reply
    arguments = {'unit_id': 'esp32_desk_led', 'teaching_id': 'happy_blink',
                 'revision': 2, 'overrides': {}}
    selected, dispatched = [], []

    def select(run, **scope):
        selected.append((run.emotion, scope))
        return arguments

    async def prepare(args, trusted):
        assert args == arguments
        assert trusted.actor == 'agent' and trusted.origin_kind == 'agent_autonomous'
        return {'unit_id': arguments['unit_id'], 'training_ref': 'happy_blink'}

    async def execute(prepared, trusted, **kwargs):
        assert kwargs['availability']()
        dispatched.append((prepared, trusted, kwargs))
        return {'result': status, **({'error': 'device_busy'} if status == 'blocked' else {})}

    monkeypatch.setattr(training_context, 'emotion_training_call', select)
    monkeypatch.setattr(tools, 'prepare_training_application', prepare)
    monkeypatch.setattr(runtime, 'run_training_behavior', execute)
    result = h.run(AIMessage(content='{"text":"听你这么说真开心。","emotion":"happy"}'))
    assert result['status'] == status and result['emotion'] == 'happy'
    assert len(dispatched) == 1 and h.calls == 1
    assert selected == [('happy', {'space_id': 'main', 'origin_kind': 'agent_autonomous'})]
    assert dispatched[0][2]['run_id'] == result['run_id'] + ':' + result['run_id'] + ':emotion'
    assert result['steps'][0]['tool'] == 'apply_training'
    assert result['steps'][0]['params'] == arguments
    assert result['steps'][0]['result']['result'] == status
    reply = next(fields for kind, fields in h.published if kind == 'agent_reply')
    assert reply['status'] == status and reply['emotion'] == 'happy'
    if status == 'ok':
        assert reply['text'] == '听你这么说真开心。'
    else:
        assert reply['text'] != '听你这么说真开心。'


@pytest.mark.parametrize('change', ['activity', 'epoch', 'mode', 'online'])
def test_activity_emotion_never_executes_after_context_changes(activity_reply, monkeypatch, change):
    from cloud.agents.common.training import context as training_context, tools

    h = activity_reply
    monkeypatch.setattr(training_context, 'emotion_training_call', lambda *_a, **_k: {
        'unit_id': 'esp32_desk_led', 'teaching_id': 'happy_blink', 'revision': 1, 'overrides': {}})

    async def no_prepare(*_args):
        pytest.fail('invalidated expression entered physical preparation')

    monkeypatch.setattr(tools, 'prepare_training_application', no_prepare)

    def changed():
        if change == 'activity':
            h.activity = {**h.activity, 'activity_id': 'new_activity'}
        elif change == 'epoch':
            h.agent._autonomy_epoch += 1
        elif change == 'mode':
            h.agent._should_act = lambda: False
        else:
            h.agent._device_online = lambda: False

    h.run(AIMessage(content='{"text":"我很开心。","emotion":"happy"}'), after_model=changed)
    assert not any(kind == 'agent_reply' for kind, _fields in h.published)


@pytest.mark.parametrize('gate', ['manual', 'user_hold'])
def test_emotion_training_rechecks_lamp_mode_and_user_hold_before_physical_step(
        activity_reply, monkeypatch, gate):
    from cloud.agents.common.training import context as training_context, tools, workflow

    h = activity_reply
    h.agent.get_autonomy_mode = lambda: 'reactive'
    h.agent._in_user_hold = lambda: False
    monkeypatch.setattr(training_context, 'emotion_training_call', lambda *_a, **_k: {
        'unit_id': 'esp32_desk_led', 'teaching_id': 'happy_blink', 'revision': 1, 'overrides': {}})

    async def prepare(args, trusted):
        # The mode/hold changed after model selection and before physical dispatch.
        if gate == 'manual':
            h.agent.get_autonomy_mode = lambda: 'manual'
        else:
            h.agent._in_user_hold = lambda: True
        return {'unit_id': args['unit_id']}

    async def check_gate(prepared, context, **kwargs):
        blocked = kwargs['before_step']({}, context)
        assert blocked == {'result': 'cancelled', 'error': 'lamp_control_changed'}
        return blocked

    monkeypatch.setattr(tools, 'prepare_training_application', prepare)
    monkeypatch.setattr(workflow, 'run_training_workflow', check_gate)
    result = h.run(AIMessage(content='{"text":"好开心。","emotion":"happy"}'))
    assert result['status'] == 'cancelled'
    assert result['steps'][0]['result']['error'] == 'lamp_control_changed'
    assert h.calls == 1
