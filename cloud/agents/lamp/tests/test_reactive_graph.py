from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from cloud.agents.lamp.workflows import reactive


class Model:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.calls = 0

    def bind_tools(self, _tools, **_kwargs):
        return self

    def invoke(self, _messages):
        self.calls += 1
        return next(self.messages)


def _agent(mode="reactive", hold=False, result=None):
    agent = SimpleNamespace()
    agent._autonomy_mode = mode
    agent._should_act = lambda: mode == "reactive" and not hold
    agent._device_online = lambda: True
    agent._find_led_unit = lambda: "esp32_desk_led"
    agent._state = object()
    agent._belief_history = []
    agent._beliefs_since_summary = 0
    agent._belief_summary = ""
    calls = []
    def dispatch(name, args, unit_id, **kwargs):
        calls.append((name, args, unit_id, kwargs))
        agent._last_reactive_tool_result = result or {"result": "ok", "task_id": "task_1"}
        return agent._last_reactive_tool_result["result"] == "ok"
    agent._dispatch_tool = dispatch
    return agent, calls


def test_manual_and_hold_skip_model_and_device(monkeypatch):
    monkeypatch.setattr(reactive, "_prompt", lambda *_args: "context")
    for mode, hold in (("manual", False), ("reactive", True)):
        model = Model([])
        agent, calls = _agent(mode, hold)
        output = reactive.run_reactive_event(agent, {"light_level": "DARK"},
            model=model, publish=lambda *_args, **_kwargs: None,
            checkpointer=InMemorySaver(), run_id=f"gate_{mode}_{hold}")
        assert output["status"] == "blocked"
        assert model.calls == 0
        assert calls == []


def test_real_result_before_activity_reply(monkeypatch):
    monkeypatch.setattr(reactive, "_prompt", lambda *_args: "context")
    from cloud.agents.lamp.personality import profile
    monkeypatch.setattr(profile, "get_profile", lambda: {"prompt": "高傲的灯"})
    model = Model([
        AIMessage(content="", tool_calls=[{"name": "set_led_state", "args": {"state": "ON"}, "id": "call_1"}]),
        AIMessage(content="完成"),
        AIMessage(content="哼，灯已经调好了。"),
    ])
    agent, calls = _agent()
    events = []
    output = reactive.run_reactive_event(agent, {"light_level": "DARK"},
        model=model, publish=lambda name, **kw: events.append((name, kw)),
        checkpointer=InMemorySaver(), run_id="auto_test_1")
    assert output["status"] == "ok"
    assert len(calls) == 1
    assert calls[0][3]["task_id"].startswith("task_")
    assert [name for name, _ in events] == ["agent_progress", "agent_reply"]
    assert events[-1][1]["scope"] == "space"


def test_unconfirmed_result_stops_and_cannot_claim_completion(monkeypatch):
    monkeypatch.setattr(reactive, "_prompt", lambda *_args: "context")
    from cloud.agents.lamp.personality import profile
    from cloud.agents.lamp.memory import taught
    monkeypatch.setattr(profile, "get_profile", lambda: {"prompt": "高傲的灯"})
    hits = []
    monkeypatch.setattr(taught, "touch", lambda rule_id: hits.append(rule_id))
    model = Model([
        AIMessage(content="", tool_calls=[{"name": "set_led_state", "args": {"state": "ON", "taught_id": "rule_1"}, "id": "call_1"}]),
        AIMessage(content="灯已经调好了。"),
    ])
    agent, calls = _agent(result={"result": "timeout", "task_id": "task_1"})
    events = []
    output = reactive.run_reactive_event(agent, {"light_level": "DARK"},
        model=model, publish=lambda name, **kw: events.append((name, kw)),
        checkpointer=InMemorySaver(), run_id="auto_test_2")
    assert len(calls) == 1
    assert hits == []
    assert output["status"] == "timeout"
    assert "回执还没到" in output["reply"]
    assert events[-1][1]["status"] == "partial"


def test_accepted_peer_request_does_not_start_another_model_turn(monkeypatch):
    monkeypatch.setattr(reactive, "_prompt", lambda *_args: "context")
    model = Model([AIMessage(content="", tool_calls=[{
        "name": "request_peer_skill", "args": {"target_unit_id": "go2",
                                               "skill_id": "greet", "params": {}},
        "id": "call_peer"}])])
    agent, calls = _agent()

    def dispatch(name, args, unit_id, **kwargs):
        calls.append((name, args, unit_id, kwargs))
        agent._last_reactive_tool_result = {"result": "accepted", "request_id": "call_peer"}
        return True

    agent._dispatch_tool = dispatch
    result = reactive.run_reactive_event(
        agent, {"light_level": "DARK"}, model=model,
        publish=lambda *_args, **_kwargs: None,
        checkpointer=InMemorySaver(), run_id="auto_peer_accepted")
    assert result["status"] == "accepted"
    assert model.calls == 1
    assert len(calls) == 1


def test_sensor_admission_debounce_and_material_signature(monkeypatch):
    from cloud.agents.lamp.personality import profile
    from cloud.agents.lamp.memory import taught
    from cloud.esp32 import tools as lamp_tools
    from cloud.space import context as space_context

    monkeypatch.setattr(profile, "get_profile", lambda: {
        "revision": 1, "traits": {"social_initiative": 70,
                                  "adjustment_sensitivity": 50}})
    monkeypatch.setattr(taught, "list_all", lambda: [])
    monkeypatch.setattr(lamp_tools, "list_peers", lambda _unit: [])
    monkeypatch.setattr(space_context, "get_space_context", lambda *_args, **_kwargs: {
        "peers": [], "recent_discoveries": []})
    agent, _calls = _agent()
    agent._last_reason_ts = 0
    agent._last_reason_sig = None
    agent._check_proactive = lambda _sense: []
    agent._sense = lambda: {"light_level": "DARK", "light_lux": 10,
                            "time_period": "night", "sound_detected": False,
                            "led_state": "OFF", "led_config": {}}
    agent._social_state = SimpleNamespace(opportunity=lambda *_args, **_kwargs: None,
                                          evaluated=lambda _item: None)
    monkeypatch.setattr(reactive, "run_reactive_event", lambda *_args, **_kwargs: {"status": "ok"})
    event = {"unit_id": "esp32_desk_light"}
    assert reactive.run_reactive_sensor_event(agent, event, now=1000) == {"status": "ok"}
    assert reactive.run_reactive_sensor_event(agent, event, now=1001) is None
    assert reactive.run_reactive_sensor_event(agent, event, now=1011) is None


def test_taught_hit_and_belief_summary_only_after_confirmed_action(monkeypatch):
    monkeypatch.setattr(reactive, "_prompt", lambda *_args: "context")
    from cloud.agents.lamp.personality import profile
    from cloud.agents.lamp.memory import taught
    monkeypatch.setattr(profile, "get_profile", lambda: {"prompt": "高傲的灯"})
    hits = []
    monkeypatch.setattr(taught, "touch", lambda rule_id: hits.append(rule_id))
    model = Model([
        AIMessage(content="", tool_calls=[{"name": "set_led_state",
            "args": {"state": "ON", "taught_id": "rule_1"}, "id": "call_1"}]),
        AIMessage(content="无需更多动作"),
        AIMessage(content="哼，灯已经调好了。"),
        AIMessage(content="晚间偏暗"),
    ])
    agent, calls = _agent()
    agent._beliefs_since_summary = 4
    output = reactive.run_reactive_event(agent, {"light_level": "DARK",
        "sound_detected": False, "led_state": "OFF"}, model=model,
        publish=lambda *_args, **_kwargs: None,
        checkpointer=InMemorySaver(), run_id="auto_taught_1")
    assert output["status"] == "ok"
    assert calls[0][1] == {"state": "ON"}
    assert hits == ["rule_1"]
    assert agent._belief_history[-1]["actions"] == ["set_led_state"]
    assert agent._belief_summary == "晚间偏暗"
    assert agent._beliefs_since_summary == 0
