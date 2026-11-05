"""Focused regression checks for autonomous speech and lamp sensor noise."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cloud.agents.common.autonomous_visibility import AutonomousReplyGate, actionable_error


def test_lamp_does_not_treat_old_light_snapshot_as_current(monkeypatch):
    from cloud.esp32 import perception
    from cloud.esp32.state import ESP32State

    state = ESP32State()
    state.on_agent_msg("esp32_desk_light", "state",
                       {"level": "DARK", "value": 2109, "ts": 100})
    monkeypatch.setattr(perception.time, "time", lambda: 1000)
    stale = perception.sense(state)
    assert stale["light_available"] is False
    assert stale["light_level"] == "UNKNOWN"
    state.on_agent_msg("esp32_desk_light", "state",
                       {"level": "DARK", "value": 2109, "ts": 990})
    fresh = perception.sense(state)
    assert fresh["light_available"] is True
    assert fresh["light_level"] == "DARK"


def test_live_light_reading_survives_unsynced_device_clock(monkeypatch):
    from cloud.esp32 import perception
    from cloud.esp32.state import ESP32State

    state = ESP32State()
    monkeypatch.setattr(perception.time, "time", lambda: 1800000000)
    state.on_agent_msg("esp32_desk_light", "state",
                       {"level": "DIM", "value": 700, "ts": 426})
    assert perception.sense(state)["light_available"] is False
    state.on_agent_msg("esp32_desk_light", "state",
                       {"level": "DIM", "value": 700, "ts": 428},
                       received_at=1799999999)
    assert perception.sense(state)["light_available"] is True
    state.on_agent_msg("esp32_desk_light", "state",
                       {"level": "DIM", "value": 700, "ts": 1799999000},
                       received_at=1799999999)
    assert perception.sense(state)["light_available"] is True


def test_lamp_first_fresh_reading_is_a_baseline(monkeypatch):
    from cloud.agents.lamp.workflows.runtime import ReactiveMind

    monkeypatch.setattr("cloud.agents.lamp.workflows.runtime.time.time", lambda: 1000)
    agent = SimpleNamespace(_last_light_level="UNKNOWN", _last_proactive_ts=0,
                            _work_start_ts=0, _last_act_ts=0)
    sense = {"light_level": "DARK", "sound_detected": False}
    assert ReactiveMind._check_proactive(agent, sense) == []
    sense["light_level"] = "DIM"
    assert ReactiveMind._check_proactive(agent, sense) == ["env_changed"]


def test_device_replies_need_completion_attention_or_material_change():
    gate = AutonomousReplyGate()
    scope = "act_work"
    assert not gate.should_reply(scope, error="reactive_preflight", now=10)
    assert gate.should_reply(scope, completed=True, material_key=("light", "DARK"), now=10)
    assert gate.mark_replied(scope, completed=True, material_key=("light", "DARK"), now=10)
    assert not gate.should_reply(scope, completed=True, material_key=("light", "DARK"), now=200)
    assert not gate.should_reply(scope, completed=True, material_key=("light", "DIM"), now=50)
    assert gate.should_reply(scope, completed=True, material_key=("light", "DIM"), now=131)
    assert gate.mark_replied(scope, completed=True, material_key=("light", "DIM"), now=131)
    assert not gate.should_reply(scope, error="repeat_action_cooldown", now=132)
    assert actionable_error({"result": "unknown", "error": "edge_result_unconfirmed"}) == "edge_result_unconfirmed"
    assert gate.mark_replied(scope, error="edge_result_unconfirmed", now=132)
    assert not gate.should_reply(scope, error="edge_result_unconfirmed", now=500)
    assert gate.should_reply("act_next", completed=True, material_key=("light", "DIM"), now=501)


def test_lamp_ignores_raw_light_and_own_led_changes(monkeypatch):
    from cloud.agents.lamp.workflows import reactive
    from cloud.agents.lamp.personality import profile as lamp_profile
    from cloud.esp32 import tools as lamp_tools
    from cloud.peers import social
    from cloud.space import context as space_context

    monkeypatch.setattr(lamp_profile, "get_profile", lambda: {
        "revision": 1, "traits": {"adjustment_sensitivity": 50,
                                   "social_initiative": 70}})
    monkeypatch.setattr(lamp_tools, "list_peers", lambda _unit: [])
    monkeypatch.setattr(space_context, "get_space_context", lambda *_args, **_kwargs: {
        "peers": [], "recent_discoveries": []})
    monkeypatch.setattr(social, "activity_peers", lambda *_args: (None, []))

    observed = {"light_level": "DARK", "light_lux": 0, "light_value": 0,
                "time_period": "上午", "sound_detected": False,
                "led_state": "OFF", "led_config": {"brightness": 0}}
    agent = SimpleNamespace(
        _state=None, _last_reason_sig=None, _last_reason_ts=0,
        _last_light_level="", _last_proactive_ts=0, _work_start_ts=0,
        _last_act_ts=0, _stable_light_level="", _pending_light_level="",
        _social_state=SimpleNamespace(opportunity=lambda *_args, **_kwargs: None),
        _should_act=lambda: True, _device_online=lambda: True,
        _find_led_unit=lambda: "esp32_desk_led", _sense=lambda: dict(observed),
        _check_proactive=lambda _sense: [],
    )
    agent._autonomy_since = 101
    assert reactive.prepare_sensor_event(agent, {"enqueue_ts": 100}, now=102) is None
    agent._autonomy_since = 0
    first = reactive.prepare_sensor_event(agent, {}, now=100)
    assert first is not None
    agent._last_reason_sig = first[1]
    agent._last_reason_ts = 100

    observed.update(light_lux=1456, light_value=1456, led_state="ON",
                    led_config={"brightness": 170})
    assert reactive.prepare_sensor_event(agent, {}, now=112) is None
    observed.update(light_lux=0, light_value=0)
    assert reactive.prepare_sensor_event(agent, {}, now=118) is None

    observed["light_level"] = "DIM"
    assert reactive.prepare_sensor_event(agent, {}, now=120) is None
    assert reactive.prepare_sensor_event(agent, {}, now=132) is None
    changed = reactive.prepare_sensor_event(agent, {}, now=136)
    assert changed is not None
    assert changed[0]["settled_light_level"] == "DIM"

    observed["light_available"] = True
    recovered = reactive.prepare_sensor_event(agent, {}, now=150)
    assert recovered is not None
    assert "light_recovered" in recovered[0]["proactive_hints"]
    assert recovered[0]["lighting_eligible"] is True


def test_lamp_blocked_autonomous_action_stays_silent(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from cloud.agents.lamp.workflows.reactive import run_reactive_event
    from cloud.space import activity as activity_module
    from cloud.space import context as space_context
    from cloud.agents.lamp.personality import profile as lamp_profile

    class Model:
        calls = 0

        def bind_tools(self, *_args, **_kwargs):
            return self

        def invoke(self, _messages):
            self.calls += 1
            return AIMessage(content="", tool_calls=[{
                "name": "set_led_state", "args": {"state": "ON"}, "id": "call_1"}])

    model = Model()
    published = []
    agent = SimpleNamespace(
        _llm=model, _state=None, _autonomy_epoch=1, _stable_light_level="DARK",
        _belief_summary="", _belief_history=[],
        _reply_gate=AutonomousReplyGate(), _last_reactive_tool_result={},
        _should_act=lambda: True, _device_online=lambda: True,
        _find_led_unit=lambda: "esp32_desk_led",
    )

    def dispatch(*_args, **_kwargs):
        agent._last_reactive_tool_result = {"result": "blocked", "error": "repeat_action_cooldown"}
        return False

    agent._dispatch_tool = dispatch
    monkeypatch.setattr(activity_module, "get_activity_store",
                        lambda: SimpleNamespace(current=lambda: None))
    monkeypatch.setattr(space_context, "get_space_context", lambda *_args, **_kwargs: {
        "peers": [], "recent_discoveries": []})
    monkeypatch.setattr(lamp_profile, "get_profile", lambda: {
        "agent_id": "esp32_desk_led", "name": "皮皮灯", "prompt": "你是智能灯。",
        "traits": {"social_initiative": 70,
                                           "adjustment_sensitivity": 50}})
    result = run_reactive_event(
        agent, {"light_level": "DARK", "lighting_eligible": True}, model=model,
        publish=lambda kind, **facts: published.append((kind, facts)),
        checkpointer=InMemorySaver())
    assert result["status"] == "blocked"
    assert model.calls == 1
    assert not any(kind == "agent_reply" for kind, _ in published)


@pytest.mark.asyncio
async def test_go2_does_not_generate_speech_for_duplicate_or_routine_failure(monkeypatch):
    from cloud.go2.mode import free_explore
    from cloud.agents.go2.workflows import reply as go2_reply
    from cloud.api import intents

    drive = free_explore.Drive()
    drive.owner_session_id = "act_work"
    drive._generation = 1
    drive._activity_id = lambda: "act_work"
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(is_connected=True))
    monkeypatch.setattr(free_explore, "get_text_llm", lambda: object())
    monkeypatch.setattr(free_explore, "get_profile", lambda: {})
    events, generated = [], []

    async def answer(**kwargs):
        generated.append(kwargs["run_id"])
        return "我已经做到了。"

    monkeypatch.setattr(go2_reply, "run_autonomous_reply_graph", answer)
    monkeypatch.setattr(intents, "publish_agent_event",
                        lambda kind, **facts: events.append((kind, facts)))
    await drive._publish_motion_reply("go2_sport", {"cmd": "Hello"}, {"result": "ok"})
    await drive._publish_motion_reply("go2_sport", {"cmd": "Hello"}, {"result": "ok"})
    await drive._publish_motion_reply("go2_sport", {"cmd": "Hello"},
                                      {"result": "blocked", "error": "action_cooldown"})
    assert len(generated) == 1
    assert [kind for kind, _ in events].count("agent_reply") == 1

    await drive._publish_motion_reply("go2_navigate_to", {"name": "home"},
                                      {"result": "unknown", "error": "stop_not_confirmed"})
    assert len(generated) == 2
    assert [kind for kind, _ in events].count("agent_reply") == 2


def test_go2_scene_observations_need_a_new_area(monkeypatch):
    from cloud.go2.mode.free_explore import Drive

    drive = Drive()
    drive.owner_session_id = "act_work"
    drive._activity_id = lambda: "act_work"
    visible = []

    def record(_kind, text, **_facts):
        visible.append(text)
        return {"event_id": f"evt_{len(visible)}"}

    drive._activity_event = record
    assert drive._record_scene_observation("看到了书桌", (0.0, 0.0))
    assert drive._record_scene_observation("书桌上还是那些东西", (0.5, 0.5)) is None
    assert drive._record_scene_observation("无可用视频帧", (3.0, 0.0)) is None
    drive._observation_gate._last_reply_at -= 121
    assert drive._record_scene_observation("到了另一边", (3.0, 0.0))
    assert visible == ["看到了书桌", "到了另一边"]
