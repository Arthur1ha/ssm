"""Firmware/Card/tool consistency and physical dispatch evidence, without devices."""

from copy import deepcopy
import ast
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import time

import pytest
from pydantic import ValidationError

from cloud.agents.lamp.tools.schemas import _physical_schema, visible_schemas
from cloud.cards.builder import build_card_from_manifest
from cloud.esp32 import physical_tasks
from cloud.esp32.card import adjust_lighting_skill, lighting_goal
from cloud.esp32.state import ESP32State
from cloud.esp32.tool_executor import execute_physical
from cloud.esp32.tool_registry import LAMP_SKILLS, TOOL_DECLARATIONS, valid_lamp_params
from cloud.space.execution import ExecutionContext
from edge import ism, lamp_contract
from edge.lamp_contract import ACTION_SCHEMAS, FSM_VERSION, capabilities

UNIT = "esp32_desk_led"


def card(*, legacy=False):
    manifest = {"unit_id": UNIT, "agent_type": "actuator", "fsm": "led",
                "firmware_ver": "0.3.0", "capabilities": capabilities()}
    if not legacy:
        manifest["fsm_version"] = FSM_VERSION
    result = build_card_from_manifest(manifest)
    result["online"] = True
    return result


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path)
    state = ESP32State()
    state.on_agent_msg(UNIT, "state", {"ism": "OFF", "brightness": 120})
    context = ExecutionContext("session_one", "admin", False,
                               expires_at=int(time.time()) + 60)
    calls = []

    def dispatch(uid, action, params, ctx, source, *, task_id):
        calls.append({"unit_id": uid, "action": action, "params": params,
                      "task_id": task_id, "session_id": ctx.session_id})
        return task_id

    def run(action="SET_STATE", params=None, task_id="request_one", **kwargs):
        return execute_physical(state=state, unit_id=UNIT, action=action,
                                params={"state": "ON"} if params is None else params,
                                context=context, dispatch=dispatch,
                                task_id=task_id, card=card(), wait=False, **kwargs)

    return SimpleNamespace(state=state, context=context, calls=calls,
                           dispatch=dispatch, run=run)


def test_current_manifest_card_and_tool_schemas_have_one_source():
    current = card()
    assert current["version"] == "0.3.0"
    skills = {skill["invoke"]["action"]: skill for skill in current["skills"]}
    for capability in capabilities():
        action = capability["action"]
        assert skills[action]["params_schema"] == capability["params_schema"]
        assert skills[action]["params_schema"] == TOOL_DECLARATIONS[LAMP_SKILLS[action]["tool"]]["params_schema"]
    topology = {(edge["src"], edge["trigger"], edge["dst"])
                for edge in current["state_machine"]["transitions"]}
    expected = {(src, trigger, dst) for (src, trigger), dst in ism.LED_TABLE.items()}
    expected.update(("BLINKING", "BLINK_DONE", target) for target in ism.BLINK_DONE_TARGETS)
    assert topology == expected
    assert current["widgets"][0]["params_schema"] == ACTION_SCHEMAS["SET_COLOR"]


def test_parameter_change_propagates_to_card_model_and_receiver(monkeypatch):
    count = ACTION_SCHEMAS["BLINK"]["properties"]["count"]
    monkeypatch.setitem(count, "maximum", 7)
    current = card()
    assert next(skill for skill in current["skills"] if skill["id"] == "blink_light")["params_schema"]["properties"]["count"]["maximum"] == 7
    assert valid_lamp_params("BLINK", {"count": 7})
    assert not valid_lamp_params("BLINK", {"count": 8})
    assert valid_lamp_params("BLINK", {"count": 20}, allow_legacy=True)
    assert next(skill for skill in card(legacy=True)["skills"] if skill["id"] == "blink_light")["params_schema"]["properties"]["count"]["maximum"] == 20
    model = _physical_schema("BlinkContractProbe", "blink_led")
    assert model(count=7).count == 7
    with pytest.raises(ValidationError):
        model(count=8)


def test_old_firmware_keeps_its_own_states_and_model_parameters():
    old = card(legacy=True)
    assert old["state_machine"]["states"] == ["OFF", "DIM", "BRIGHT", "COLOR", "BLINK", "ERROR"]
    schema = visible_schemas(lighting_only=True, card=old)["set_led_state"]
    assert schema(state="DIM").state == "DIM"
    with pytest.raises(ValidationError):
        schema(state="ON")


def test_mismatched_or_unknown_firmware_contract_is_not_advertised():
    manifest = {"unit_id": UNIT, "fsm": "led", "fsm_version": FSM_VERSION,
                "agent_type": "actuator", "capabilities": deepcopy(capabilities())}
    manifest["capabilities"][0]["params_schema"]["properties"]["r"]["maximum"] = 999
    with pytest.raises(ValueError, match="parameter contract"):
        build_card_from_manifest(manifest)
    manifest["fsm_version"] = 999
    with pytest.raises(ValueError, match="unsupported lamp fsm_version"):
        build_card_from_manifest(manifest)


def test_semantic_goal_receiver_uses_public_contract():
    skill = adjust_lighting_skill(UNIT)
    assert lighting_goal({"goal": " 阅读 "}) == "阅读"
    with pytest.raises(ValueError):
        lighting_goal({"goal": "阅读", "color": "blue"})
    with pytest.raises(ValueError):
        lighting_goal({"goal": "a" * (skill["params_schema"]["properties"]["goal"]["maxLength"] + 1)})


def test_retries_read_original_record_and_different_parameters_reject(boundary):
    assert boundary.run()["result"] == "accepted"
    assert boundary.run()["result"] == "unknown"
    assert boundary.run(params={"state": "OFF"})["error"] == "physical_task_id_conflict"
    assert len(boundary.calls) == 1
    physical_tasks.record_result("request_one", UNIT, {"result": "ok"})
    assert boundary.run()["result"] == "ok"
    assert boundary.run(task_id="request_two")["result"] == "accepted"
    assert len(boundary.calls) == 2


def test_omitted_brightness_is_stable_across_retry(boundary):
    params = {"r": 20, "g": 30, "b": 40}
    assert boundary.run("SET_COLOR", params)["result"] == "accepted"
    assert boundary.calls[0]["params"]["brightness"] == 120
    boundary.state.on_agent_msg(UNIT, "state", {"ism": "ON", "brightness": 220})
    assert boundary.run("SET_COLOR", params)["result"] == "unknown"
    assert len(boundary.calls) == 1


def test_rejection_happens_before_reservation_or_publish(boundary):
    assert boundary.run("BLINK", {"count": 21})["result"] == "rejected"
    assert boundary.calls == []
    assert physical_tasks.get("request_one", unit_id=UNIT, session_id="session_one") is None
    boundary.state.on_agent_msg(UNIT, "state", {"ism": "UNKNOWN"})
    assert boundary.run()["result"] == "blocked"
    assert boundary.calls == []


def test_publish_failure_stays_unknown_and_does_not_replay(boundary):
    calls = []

    def lost_publish(*args, **kwargs):
        calls.append(kwargs["task_id"])
        raise OSError("connection lost after publish")

    for _ in range(2):
        result = execute_physical(state=boundary.state, unit_id=UNIT, action="SET_STATE",
                                  params={"state": "ON"}, context=boundary.context,
                                  dispatch=lost_publish, task_id="lost", card=card(), wait=False)
        assert result["result"] == "unknown"
    assert calls == ["lost"]


def test_result_identity_and_session_are_checked(boundary):
    boundary.run()
    assert not physical_tasks.record_result("request_one", UNIT,
                                            {"result": "ok", "unit_id": "other"})
    assert not physical_tasks.record_result("request_one", UNIT,
                                            {"result": "ok", "session_id": "other"})
    assert physical_tasks.get("request_one", unit_id=UNIT, session_id="other") is None
    assert boundary.run()["result"] == "unknown"


def test_api_ingress_rejects_foreign_results_before_terminal_cache(boundary, monkeypatch):
    from cloud.api import intents, main
    boundary.run()
    noop = SimpleNamespace(handle_message=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_esp32_state", boundary.state)
    monkeypatch.setattr(main, "get_registry", lambda: noop)
    monkeypatch.setattr(main, "get_constraint_store", lambda: noop)
    monkeypatch.setattr(main, "get_activity_store", lambda: noop)
    monkeypatch.setattr(main, "_agent_messenger", None)
    monkeypatch.setattr(main.peer_gateway, "handle_peer_result", lambda *_args: False)
    monkeypatch.setattr(intents, "_lamp_task_owners", {})
    monkeypatch.setattr(intents, "_lamp_task_results", {})

    def receive(payload, *, unit=UNIT, task="request_one"):
        main._on_esp32_message(None, None, SimpleNamespace(
            topic=f"ssm/result/{unit}/{task}", payload=json.dumps(payload).encode()))

    for override in ({"unit_id": "other"}, {"task_id": "other"},
                     {"session_id": "other"}, {"result": "invented"}):
        receive({"result": "ok", **override})
    receive({"result": "ok"}, unit="other")
    assert boundary.state.get_task_result("request_one") is None
    assert boundary.run()["result"] == "unknown"
    receive({"result": "ok", "session_id": "session_one"})
    assert boundary.state.get_task_result("request_one")["result"] == "ok"
    receive({"result": "error", "session_id": "session_one"})
    assert boundary.state.get_task_result("request_one")["result"] == "ok"
    # Preserve legitimately registered requests sent before this migration.
    intents._lamp_task_owners["legacy"] = (UNIT, "legacy_session")
    receive({"result": "ok", "session_id": "wrong"}, task="legacy")
    assert boundary.state.get_task_result("legacy") is None
    receive({"result": "ok"}, task="legacy")
    assert boundary.state.get_task_result("legacy")["result"] == "ok"
    receive({"result": "ok"}, task="unregistered")
    assert boundary.state.get_task_result("unregistered") is None


def test_coordinator_ingress_persists_after_restart_without_replaying(boundary):
    from cloud.orchestrator.shared_state import SharedState
    boundary.run()
    # main.py intentionally starts its MQTT loop on import. Extract the actual
    # callback only, so this check exercises ingress without connecting a broker.
    path = Path(__file__).parents[1] / "orchestrator/main.py"
    module = ast.parse(path.read_text())
    callback = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                    and node.name == "on_message")
    fresh_state = SharedState()
    namespace = {"json": json, "time": time, "state": fresh_state,
                 "logger": logging.getLogger(__name__), "_agent_messenger": None,
                 "CURRENT_CONSTRAINTS_TOPIC": "ssm/constraints/current",
                 "CURRENT_ACTIVITY_TOPIC": "ssm/activity/current",
                 "registry": SimpleNamespace(handle_message=lambda *_args, **_kwargs: None)}
    exec(compile(ast.Module(body=[callback], type_ignores=[]), str(path), "exec"), namespace)

    def receive(task, payload):
        namespace["on_message"](None, None, SimpleNamespace(
            topic=f"ssm/result/{UNIT}/{task}", payload=json.dumps(payload).encode()))

    receive("request_one", {"result": "ok", "session_id": "other"})
    assert boundary.run()["result"] == "unknown"
    receive("request_one", {"result": "ok", "session_id": "session_one"})
    assert boundary.run()["result"] == "ok"
    assert len(boundary.calls) == 1
    fresh_state.register_task("legacy", "legacy_session")
    receive("legacy", {"result": "ok", "task_id": "wrong"})
    assert fresh_state.get_task_result("legacy") is None
    receive("legacy", {"result": "ok", "session_id": "legacy_session"})
    assert fresh_state.get_task_result("legacy")["result"] == "ok"
    receive("unregistered", {"result": "ok"})
    assert fresh_state.get_task_result("unregistered") is None


def test_executing_and_timeout_do_not_mean_blink_completed(boundary):
    def dispatch(uid, action, params, ctx, source, *, task_id):
        boundary.state.store_task_result(task_id, {"result": "executing"})
        return boundary.dispatch(uid, action, params, ctx, source, task_id=task_id)

    result = execute_physical(state=boundary.state, unit_id=UNIT, action="BLINK",
                              params={"count": 2}, context=boundary.context, dispatch=dispatch,
                              task_id="blink", card=card(), timeout=0.001)
    assert result["result"] == "timeout"
    assert physical_tasks.get("blink", unit_id=UNIT, session_id="session_one")["result"] == "unknown"
    physical_tasks.record_result("blink", UNIT, {"result": "ok", "ism_state": "OFF"})
    assert boundary.run("BLINK", {"count": 2}, task_id="blink")["result"] == "ok"
    assert len(boundary.calls) == 1


def test_waiting_blink_does_not_block_a_user_stop(boundary, monkeypatch):
    waiting, cancelled = threading.Event(), threading.Event()
    outcome = []

    def wait_result(*_args, **_kwargs):
        waiting.set()
        return {"result": "cancelled" if cancelled.wait(2) else "timeout"}

    monkeypatch.setattr(boundary.state, "wait_task_result", wait_result)

    def blink():
        outcome.append(execute_physical(
            state=boundary.state, unit_id=UNIT, action="BLINK", params={"count": 2},
            context=boundary.context, dispatch=boundary.dispatch,
            task_id="waiting_blink", card=card()))

    worker = threading.Thread(target=blink)
    worker.start()
    try:
        assert waiting.wait(1)
        assert boundary.run(params={"state": "OFF"}, task_id="stop")["result"] == "accepted"
        cancelled.set()
    finally:
        worker.join(3)
    assert outcome[0]["result"] == "cancelled"


@pytest.mark.parametrize("restore_fails,initial", [(False, "OFF"), (True, "OFF"), (False, "ERROR")])
def test_edge_blink_success_requires_restore_and_matching_feedback(monkeypatch, restore_fails, initial):
    monkeypatch.setitem(sys.modules, "clock", SimpleNamespace(unix_time=time.time))
    monkeypatch.setitem(sys.modules, "ism", ism)
    monkeypatch.setitem(sys.modules, "lamp_contract", lamp_contract)
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(
        DEVICE_ID="esp32_desk", UNIT_LIGHT="light", UNIT_IR="ir", UNIT_SOUND="sound", UNIT_LED=UNIT))
    module_spec = importlib.util.spec_from_file_location("lamp_bridge_check", Path(__file__).parents[2] / "edge/trigger_map.py")
    bridge_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(bridge_module)
    events = []

    def restore(state):
        events.append(("restore", state))
        if restore_fails:
            raise OSError("pixels unavailable")

    bsm = SimpleNamespace(led_blink=lambda *_args: "blink_1", led_set_state=restore)
    mqtt = SimpleNamespace(publish=lambda topic, payload, **_kwargs: events.append((topic, deepcopy(payload))))
    machine = ism.ISM(initial, UNIT, ism.LED_TABLE)
    bridge = bridge_module.TriggerMap(bsm, machine, None, None, mqtt)
    bridge.on_mqtt(f"ssm/task/{UNIT}/blink", {"session_id": "s", "action": "BLINK", "params": {"count": 2}})
    assert events[-1][1]["result"] == "executing"
    bridge.on_bsm_event("BLINK_DONE", {"blink_id": "old"})
    assert machine.state == "BLINKING"
    bridge.on_bsm_event("BLINK_DONE", {"blink_id": "blink_1"})
    assert events[-3] == ("restore", "OFF")
    assert events[-2][1]["ism"] == ("ERROR" if restore_fails else "OFF")
    assert events[-1][1]["result"] == ("error" if restore_fails else "ok")
