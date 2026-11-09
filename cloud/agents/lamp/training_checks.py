from cloud.agents.common.training.check_fixtures import lamp_sound_fixture
from cloud.agents.common.training.policy import training_event_intent
from types import SimpleNamespace
import time

import pytest


def test_sound_event_keeps_bound_count_and_requires_off():
    f = lamp_sound_fixture()
    result = training_event_intent(f['event'], f['records'], f['catalog'], f['facts'], now=100)
    assert result['params'] == {'count': 2}
    assert not training_event_intent(f['event'], f['records'], f['catalog'],
                                     {**f['facts'], 'state': {'ism': 'ON'}}, now=100)
    assert not training_event_intent(f['event'], f['records'], f['catalog'], f['facts'], now=106)


@pytest.mark.parametrize('selected,count', [('reading', 2), ('music', 3), (None, None), ('unknown', None)])
def test_sensor_graph_semantically_selects_one_saved_candidate(monkeypatch, selected, count):
    from langgraph.checkpoint.memory import InMemorySaver
    from cloud.agents.lamp.workflows import reactive

    class Model:
        selections = 0

        def bind_tools(self, *_args, **_kwargs):
            return self

        def with_structured_output(self, *_args, **_kwargs):
            return self

        def invoke(self, messages):
            self.selections += 1
            return {'teaching_id': selected, 'reason': '依据当前用户活动理解情境'}

    model = Model()
    sent = []
    agent = SimpleNamespace(
        _state=None, _autonomy_epoch=1, _stable_light_level='DARK',
        _belief_summary='', _belief_history=[], _beliefs_since_summary=0,
        _find_led_unit=lambda: 'esp32_desk_led',
        _should_act=lambda: True, _device_online=lambda: True,
        _reply_gate=SimpleNamespace(should_reply=lambda *_a, **_k: False),
    )

    def dispatch(tool, params, unit, **kwargs):
        sent.append((tool, params, kwargs['training_intent']))
        agent._last_reactive_tool_result = {'result': 'ok'}
        return True

    agent._dispatch_tool = dispatch
    monkeypatch.setattr('cloud.space.context.get_space_context', lambda *_a, **_k: {
        'peers': [], 'activity': {'text': '用户刚说明准备看会儿书'}})
    from cloud.esp32.capability_chain_checks import card
    monkeypatch.setattr('cloud.cards.registry.get_registry', lambda: SimpleNamespace(get_card=lambda _: card()))
    deadline = time.time() + 30
    candidates = [{'teaching_id': teaching_id, 'revision': 1, 'transition_id': 'off-blink',
                   'situation': situation, 'params': {'count': value}, 'event_id': 'sound:1',
                   'deadline': deadline, 'autonomy_epoch': 1}
                  for teaching_id, situation, value in [('reading', '看书时', 2), ('music', '听音乐时', 3)]]
    result = reactive.run_reactive_event(
        agent, {'light_level': 'DARK', 'lighting_eligible': True, 'training_candidates': candidates},
        model=model, publish=lambda *_a, **_k: None, checkpointer=InMemorySaver())
    assert model.selections == 1
    if count is None:
        assert result['status'] == 'blocked' and sent == []
    else:
        assert result['status'] == 'ok' and len(sent) == 1
        assert sent[0][0:2] == ('blink_led', {'count': count})
        assert sent[0][2]['teaching_id'] == selected
        assert sent[0][2]['situation_confirmed'] is True
