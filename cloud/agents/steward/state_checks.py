"""The steward reads one fresh, admitted MQTT observation per unit."""

from types import SimpleNamespace

import pytest

from cloud.agents.steward.tools import make_executor, transport
from cloud.agents.steward import tools
from cloud.agents.steward.tools import state as state_view


NOW = 1000.0


def cache(sensors=None, actuators=None):
    return SimpleNamespace(sensor_snapshot=lambda: sensors or {},
                           actuator_snapshot=lambda: actuators or {})


def test_reclassified_lamp_has_one_latest_observation_with_hardware_values():
    state = cache(
        {"esp32_desk_led": {"state": {"ism": "OFF", "brightness": 0, "ts": NOW - 500}}},
        {"esp32_desk_led": {"state": {"ism": "ON", "r": 255, "g": 190, "b": 125,
            "brightness": 180, "ts": 10, "_received_at": NOW - 1,
            "task_id": "private-task", "control_state": "internal"}}})
    observed = state_view.cached_observations(state, cards={
        "esp32_desk_led": {"device_id": "esp32_desk", "agent_type": "actuator"}},
        device_ids={"esp32_desk"}, now=NOW)
    assert observed == [{"source": "esp32_desk_led", "status": "current", "as_of": NOW - 1,
                         "values": {"ism": "ON", "r": 255, "g": 190, "b": 125, "brightness": 180}}]


def test_cache_scope_uses_card_ownership_and_sensor_device_boundary():
    entries = {unit: {"state": {"value": 5, "ts": NOW - 1}} for unit in (
        "esp32_desk_light", "esp32_desktop_light", "esp32_desk_evil_light",
        "other_light", "esp32_desk_hidden", "go2")}
    observations = state_view.cached_observations(cache(entries), cards={
        "go2": {"device_id": "go2", "agent_type": "robot"},
        "esp32_desk_hidden": {"device_id": "other", "agent_type": "actuator"}},
        device_ids={"esp32_desk"}, now=NOW)
    assert [entry["source"] for entry in observations] == ["esp32_desk_light"]


def test_stale_unknown_and_duplicate_state_do_not_gain_freshness():
    state = cache(
        {"esp32_desk_light": {"state": {"value": 99, "ts": NOW - 1}},
         "esp32_desk_sound": {"state": {"detected": True, "ts": NOW - 100}},
         "esp32_desk_ir": {"state": {"presence": True, "_received_at": float("nan"), "ts": NOW}}},
        {"esp32_desk_light": {"state": {"value": 0, "ts": NOW - 100}}})
    observed = {row["source"]: row for row in state_view.cached_observations(
        state, cards={}, device_ids={"esp32_desk"}, now=NOW)}
    assert observed["esp32_desk_light"]["values"] == {"value": 99}
    assert observed["esp32_desk_sound"] == {
        "source": "esp32_desk_sound", "status": "stale", "as_of": NOW - 100}
    assert observed["esp32_desk_ir"] == {"source": "esp32_desk_ir", "status": "unknown"}


def test_cache_projection_is_bounded_after_membership_filter():
    sensors = {f"outside_{i:02}": {"state": {"ts": NOW}} for i in range(40)}
    sensors.update({f"esp32_desk_sensor{i:02}": {"state": {"ts": NOW}} for i in range(30)})
    observed = state_view.cached_observations(cache(sensors), cards={},
        device_ids={"esp32_desk"}, now=NOW)
    assert len(observed) == 24
    assert all(row["source"].startswith("esp32_desk_") for row in observed)


@pytest.mark.asyncio
async def test_read_state_tool_returns_cache_metadata_without_raw_payload(monkeypatch):
    monkeypatch.setattr(state_view, "get_space_registry", lambda: SimpleNamespace(
        get_space=lambda: {"device_ids": ["esp32_desk"]}))
    monkeypatch.setattr(state_view.time, "time", lambda: NOW)
    monkeypatch.setattr(transport, "_state", cache({
        "esp32_desk_light": {"state": {"value": 818, "ts": NOW - 1},
                             "event": {"private": "event"}}}))
    monkeypatch.setattr(transport, "_registry", SimpleNamespace(get_all_cards=lambda: {}))
    run = SimpleNamespace(run_id="read", session_id="session", steps=[])
    execute = make_executor(run, {"space_id": "main"})
    result = await execute("read_state", {}, "read-step")
    assert result == {"result": "ok", "source": "mqtt_cache", "as_of": NOW,
        "observations": [{"source": "esp32_desk_light", "status": "current", "as_of": NOW - 1,
                          "values": {"value": 818}}]}


def test_steward_prompt_context_and_state_tool_share_filtered_observations(monkeypatch):
    monkeypatch.setattr(state_view, "get_space_registry", lambda: SimpleNamespace(
        get_space=lambda: {"device_ids": ["esp32_desk"]}))
    monkeypatch.setattr(state_view, "get_space_context", lambda *_a, **_k: {
        "peers": [], "environment": [{"source": "other", "status": "current",
            "as_of": NOW, "values": {"value": 123}}], "constraints": [],
        "activity": None, "recent_discoveries": [], "decision_evidence": ["old"]})
    state = cache({"esp32_desk_light": {"state": {"value": 818, "ts": NOW - 1}}})
    registry = SimpleNamespace(get_all_cards=lambda: {})
    context = state_view.steward_space_context(state=state, registry=registry, now=NOW)
    assert context["environment"] == state_view.read_cached_state(
        state, registry=registry, now=NOW)["observations"]
    assert len(context["decision_evidence"]) == 1
    assert context["decision_evidence"][0]["source"] == "esp32_desk_light"


@pytest.mark.asyncio
@pytest.mark.parametrize("unit_location", ["result", "params", "missing"])
async def test_await_result_queries_original_mqtt_task_with_target(monkeypatch, unit_location):
    calls = []

    def wait(task_id, session_id, deadline, *, unit_id):
        calls.append((task_id, session_id, unit_id))
        return {"result": "ok", "task_id": task_id}

    monkeypatch.setattr(tools, "_wait_mqtt_result", wait)
    step = {"tool": "invoke_skill", "params": {},
            "result": {"task_id": "physical-original", "result": "unknown"}}
    if unit_location != "missing":
        step[unit_location]["unit_id"] = "esp32_desk_led"
    run = SimpleNamespace(run_id="await", session_id="session", steps=[step], deadline=2000)
    execute = make_executor(run, {"space_id": "main"})
    result = await execute("await_result", {"task_id": "physical-original"}, "await-step")
    if unit_location == "missing":
        assert result == {"result": "rejected", "error": "task_target_unavailable"}
        assert calls == []
    else:
        assert result == {"result": "ok", "task_id": "physical-original"}
        assert calls == [("physical-original", "session", "esp32_desk_led")]
