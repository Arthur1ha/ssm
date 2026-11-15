"""New activity decisions use their final text without a second reply model."""
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from cloud.agents.lamp.workflows.reactive import run_reactive_event


@pytest.fixture
def activity_reply(monkeypatch):
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
    monkeypatch.setattr('cloud.space.activity.get_activity_store', lambda:
                        SimpleNamespace(current=lambda: state.activity))
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
        'source': 'autonomous', 'status': 'ok', 'run_id': result['run_id'], 'text': answer})]


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
