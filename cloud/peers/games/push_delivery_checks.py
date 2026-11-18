"""Small mocked delivery checks: ordering and receipts, without real devices."""
from __future__ import annotations

import asyncio
import sys
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from cloud.peers.games import get_game, playable_games, push_delivery
from cloud.peers.games.delivery_decisions import (
    Agreement, CompletionDecision, DestinationDecision, InvitationDecision, NextDecision,
)
from cloud.space.execution import ExecutionContext


def _stub_module(monkeypatch, name, **attributes):
    module = ModuleType(name)
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, name, module)


def test_registered_game_is_playable_in_both_directions_without_pusher(monkeypatch):
    from cloud.peers.games import registry

    _stub_module(monkeypatch, "cloud.go2.tools.registry", get_tool_spec=lambda _name: object())
    monkeypatch.setattr(registry, "activity_allows", lambda *_args: True)
    monkeypatch.setattr(registry, "game_invitation_ready", lambda *_args: True)
    game = get_game("push_delivery")
    assert game is push_delivery.GAME and game.bidirectional
    for pair in (game.participants, game.participants[::-1]):
        assert game.available(*pair)
        assert "push_delivery" in {entry["game_id"] for entry in playable_games("act_delivery", *pair)}
    assert not game.available("unknown_device", "go2")
    assert "不要求安装推板" in game.rules


def test_destination_and_push_decisions_reject_incomplete_or_invalid_parameters():
    valid = {"accepted": True, "speech": "把这件货物送到红色地垫。",
             "destination": "红色地垫", "color_name": "红色",
             "color": {"r": 255, "g": 0, "b": 0, "brightness": 160},
             "evidence_refs": ["evt_scene"]}
    assert DestinationDecision.model_validate(valid).model_dump()["color"] == valid["color"]
    for invalid in ({**valid, "destination": " "}, {**valid, "evidence_refs": []},
                    {**valid, "evidence_refs": [" "]}, {**valid, "color": None},
                    {**valid, "color": {"r": 256, "g": 0, "b": 0, "brightness": 160}},
                    {**valid, "color": {"r": 0, "g": 0, "b": 0, "brightness": 160}}):
        with pytest.raises(ValidationError):
            DestinationDecision.model_validate(invalid)
    for distance in (0, -1, float('inf'), float('nan')):
        with pytest.raises(ValidationError):
            NextDecision(action="push", speech="尝试短距推送。", distance_m=distance)


def test_internal_delivery_choices_can_be_silent_but_invitation_and_final_need_a_response():
    assert Agreement(accepted=True).speech == ""
    assert NextDecision(action="observe").speech == ""
    assert NextDecision(action="push", distance_m=.4).speech == ""
    assert NextDecision(action="question", question="目的地是哪块？").question == "目的地是哪块？"
    for model, params in ((InvitationDecision, {"accepted": True}),
                          (CompletionDecision, {"action": "finish"})):
        with pytest.raises(ValidationError):
            model(**params)


class _MemorySaver(InMemorySaver):
    @asynccontextmanager
    async def async_run_lock(self, _thread):
        yield


@pytest.fixture
async def delivery_harness(monkeypatch):
    loop = asyncio.get_running_loop()

    def inline_executor(_executor, callback, *args):
        # The coordinator's synchronous graph callbacks only route mock data.
        # Avoid creating an actual executor for this fully mocked check.
        future = loop.create_future()
        try:
            future.set_result(callback(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    monkeypatch.setattr(loop, "run_in_executor", inline_executor)
    calls, events, trace, search_ids, cargo_contexts = [], [], [], [], []
    search_evidence = {"frame_id": "found_cargo_frame", "target": "地上的小纸盒"}
    cargo_location = {"x": 1., "y": 0., "source": "visual_distance_estimate"}
    config = SimpleNamespace(push_result="ok", destination="红色地垫", directional=False)
    lamp_color = {"rgb": (40, 80, 120), "brightness": 160}
    agent = SimpleNamespace(
        _autonomy_epoch=1, _command_epoch=1, _peer_game_id=None,
        _should_act=lambda: True, _device_online=lambda: True,
        _state=SimpleNamespace(actuator_snapshot=lambda: {
            push_delivery.LAMP: {"state": {"ism": "ON"}}}),
    )

    def begin(owner):
        agent._peer_game_id = owner
        return True

    def end(owner):
        assert owner == agent._peer_game_id
        agent._peer_game_id = None

    agent.begin_peer_game, agent.end_peer_game = Mock(side_effect=begin), Mock(side_effect=end)
    messenger = Mock()
    messenger.get_status.return_value = {"state": "ok"}
    navigator = SimpleNamespace(stop_and_confirm=AsyncMock(return_value={"result": "ok"}))
    drive = SimpleNamespace(
        _context=ExecutionContext("peer_test_delivery", "visitor", False,
                                  origin_kind="agent_autonomous", expires_at=int(time.time()+60)),
        _activity_id=lambda: "act_delivery", user_interrupt=False,
        _active_goal={"task_id": "task_delivery"},
        _advance_task=AsyncMock(return_value={"result": {"result": "ok"}}),
    )

    def record(_activity, kind, **details):
        entry = {"event_id": f"evt_delivery_{len(events)}", "event_type": kind, **details}
        events.append(entry)
        return entry

    def space_context(*_args, **_kwargs):
        return {"decision_evidence": [
            {"ref": entry["event_id"], "kind": "observation", "source": "go2",
             "as_of": time.time(), "summary": entry["text"]}
            for entry in events if entry["event_type"] == "observation"]}

    def resolve_decision(space, reason, refs, **_kwargs):
        available = {entry["ref"]: entry for entry in space["decision_evidence"]}
        if not refs or not all(ref in available for ref in refs):
            raise ValueError("decision_evidence_unavailable")
        return {"reason": reason, "evidence": [available[ref] for ref in refs]}

    async def decide(role, phase, facts, **_kwargs):
        stage = facts.get("stage")
        trace.append(("decision", role, phase, stage))
        if phase == "agree":
            return {"accepted": True, "speech": "同意这次分工和已明确的约定。"}
        if phase == "cargo":
            return {"accepted": True, "cargo": "地上的小纸盒", "speech": "请先找地上的小纸盒。"}
        if phase == "destination":
            assert facts["cargo_receipt"]["found"] is True
            assert facts["courier_report"]["found"] is True
            return {"accepted": True, "destination": config.destination, "color_name": "红色",
                    "color": {"r": 255, "g": 0, "b": 0, "brightness": 160},
                    "evidence_refs": [facts["observation"]["ref"]],
                    "speech": f"送到{config.destination}附近，这单约定用红光表示。"}
        assert role == "go2" and phase == "next"
        if stage == "waiting_destination":
            assert facts["destination_confirmed"] is False
            return {"action": "question", "question": "我找到货物了，这单送去哪里？",
                    "distance_m": None, "speech": "货物找到了，我等你指定目的地。"}
        assert facts["destination_confirmed"] is True
        if stage == "delivered":
            assert facts["latest_push"]["delivered"] is True
            assert facts["latest_push"]["confirmation"] == "cargo_near_destination_and_stopped"
            assert facts["latest_push"]["stop"]["result"] == "ok"
            return {"action": "finish", "question": "", "distance_m": None,
                    "speech": "同一货物已确认在指定标识附近，这单大致送到了。"}
        if stage == 'directional_push_completed':
            assert facts['latest_push']['directional_push_attempted'] is True
            assert facts['latest_push']['delivered'] is False
            return {'action': 'finish', 'question': '', 'distance_m': None,
                    'speech': '我已经朝指定标识的方向尝试推了一下并停稳，还没有确认货物送达。'}
        assert stage == "delivery"
        return {"action": "push", "question": "", "distance_m": 0.15,
                "speech": "我沿已确认的方向短距推送这件货物。"}

    @contextmanager
    def search_request(request_id, _deadline, *, require_cargo_measurement=False):
        if request_id.endswith('_cargo'):
            assert require_cargo_measurement is True
        search_ids.append(request_id)
        try:
            yield
        finally:
            search_ids.pop()

    @contextmanager
    def expect_cargo(reference, *, request_id=None, signal_rgb=None):
        cargo_contexts.append({"reference": deepcopy(reference), "request_id": request_id,
                               "signal_rgb": signal_rgb})
        yield

    async def execute_tool(name, params, **_kwargs):
        trace.append(("tool", name))
        calls.append((name, deepcopy(params)))
        if name == "go2_observe":
            return {"result": "ok", "message": f"地面有一个小纸盒，旁边有可辨认的{config.destination}。"}
        if name == "go2_find_object":
            return {"result": "ok", "outcome": "nearby", "found": True,
                    "target": params["target"], "task_id": search_ids[-1], "stop": {"result": "ok"},
                    "cargo_location": deepcopy(cargo_location),
                    "evidence": deepcopy(search_evidence)}
        assert name == "go2_push_cargo"
        if config.directional:
            return {'result': config.push_result, 'delivered': False,
                    'directional_push_attempted': True, 'confirmation': 'directional_move_and_stop',
                    'stop': {'result': 'ok'}, 'reference': {'cargo': params['cargo'], 'destination': params['destination']}}
        return {"result": config.push_result, "delivered": config.push_result == "ok",
                "confirmation": "cargo_near_destination_and_stopped",
                "task_id": search_ids[-1], "stop": {"result": "ok"},
                "reference": {"cargo": params["cargo"], "destination": params["destination"]}}

    def execute_physical(**kwargs):
        trace.append(("physical", kwargs["action"]))
        calls.append((kwargs["action"], deepcopy(kwargs["params"])))
        if kwargs["action"] == "SET_COLOR":
            color = kwargs["params"]
            lamp_color.update(rgb=tuple(color[key] for key in ("r", "g", "b")),
                              brightness=color["brightness"])
        return {"result": "ok", "task_id": kwargs["task_id"]}

    async def invoke_blocking(callback, *args, **kwargs):
        return callback(*args, **kwargs)

    async def run_device_task(role, goal, *, context, run_id, allowed_tools,
                              tool_context=None, result_transform=None, tool_guard=None, **_kwargs):
        trace.append(("brain_task", role, run_id))
        if role == "lamp":
            color = _kwargs["task_context"]()["order"]["color"]
            assert allowed_tools == frozenset({"set_led_state", "set_led_color"})
            assert tool_guard("set_led_color", color) is None
            receipt = execute_physical(action="SET_COLOR", params=color, task_id=run_id)
            return {"result": "ok", "tool_results": [{"tool": "set_led_color", "result": receipt}]}
        name = ("go2_push_cargo" if "go2_push_cargo" in allowed_tools else
                "go2_find_object" if "go2_find_object" in allowed_tools else "go2_observe")
        if name == "go2_push_cargo":
            params = dict(_kwargs["task_context"]()["parameters"])
            response = ("我已经朝指定标识的方向尝试推了一下并停稳，还没有确认货物送达。"
                        if config.directional else "同一货物已确认在指定标识附近，这单大致送到了。")
        elif name == "go2_find_object":
            params = {"target": _kwargs["task_context"]()["cargo"]}
            params = _kwargs["tool_declarations"][name].model_validate(params).model_dump()
            response = "我找到货物了，这单送去哪里？"
        else:
            params, response = {"question": goal}, ""
        assert tool_guard is None or tool_guard(name, params) is None
        manager = tool_context(name, params, run_id + ":step") if tool_context else nullcontext()
        with manager:
            receipt = await execute_tool(name, params, context=context)
        if result_transform:
            receipt = result_transform(name, params, receipt)
        return {"result": receipt["result"], "response": response,
                "tool_results": [{"tool": name, "result": receipt}]}

    stubs = {
        "cloud.api.intents": {"_dispatch_mqtt_task": Mock()},
        "cloud.cards.registry": {"get_registry": lambda: SimpleNamespace(get_card=lambda _uid: {})},
        "cloud.esp32.service": {"get_agent": lambda: agent},
        "cloud.esp32.tool_executor": {"execute_physical": execute_physical},
        "cloud.go2.connection": {"go2": SimpleNamespace(is_connected=True, _odom_session_id="delivery-odom")},
        "cloud.go2.control": {"control_lease": SimpleNamespace(snapshot=lambda: None)},
        "cloud.go2.navigation.navigator": {"navigator": navigator},
        "cloud.go2.navigation.cargo_push": {"expect_cargo": expect_cargo},
        "cloud.go2.tools.registry": {"get_tool_spec": lambda _name: SimpleNamespace(description="推物工具的当前契约")},
        "cloud.go2.tools.executor": {"execute_tool": execute_tool},
        "cloud.space.activity": {"get_activity_store": lambda: SimpleNamespace(record_event=record,
            get=lambda _aid: {"end_reason": "user_intervention"})},
        "cloud.space.context": {"get_space_context": space_context},
        "cloud.space.decisions": {"resolve_decision": resolve_decision,
                                  "decision_evidence": lambda space: space.get("decision_evidence", [])},
        "cloud.agents.go2.workflows.search": {"search_request": search_request},
        "cloud.peers.games.color_hunt": {"live_lamp_color": lambda _uid: deepcopy(lamp_color)},
        "cloud.peers.games.role_graph": {"run_device_task": run_device_task},
    }
    for name, attributes in stubs.items():
        _stub_module(monkeypatch, name, **attributes)
    from cloud.peers.games import base
    monkeypatch.setattr(base, "activity_allows", lambda *_args: True)
    monkeypatch.setattr(push_delivery, "decide", decide)
    monkeypatch.setattr(push_delivery, "FileCheckpointSaver", _MemorySaver)
    event = {"message_id": "msg_delivery", "messenger": messenger,
             "activity_id": "act_delivery", "deadline": time.time()+60,
             "message": {"sender": push_delivery.LAMP, "content": "来玩指挥官与送货员。"},
             "acceptance_message_id": "msg_delivery_acceptance"}
    return SimpleNamespace(config=config, event=event, drive=drive, messenger=messenger,
                           agent=agent, navigator=navigator, calls=calls, events=events, trace=trace,
                           cargo_contexts=cargo_contexts, search_evidence=search_evidence,
                           cargo_location=cargo_location)


@pytest.mark.parametrize("reason", ["delivery_service_unavailable", "lamp_busy"])
async def test_preflight_failure_keeps_announced_task_id(delivery_harness, monkeypatch, reason):
    h = delivery_harness
    if reason == "delivery_service_unavailable":
        monkeypatch.setattr(sys.modules["cloud.esp32.service"], "get_agent", lambda: None)
    else:
        h.agent.begin_peer_game = Mock(return_value=False)
    await push_delivery.GAME.execute(h.drive, h.event, "task_delivery")
    h.messenger.send_result.assert_called_once_with(
        h.event["message_id"], "blocked", task_id="task_delivery", reason=reason)
    assert not h.calls
    h.drive._advance_task.assert_not_awaited()


@pytest.mark.parametrize('destination', ['红色地垫', '白色椅子脚旁'])
async def test_delivery_orders_find_report_destination_signal_and_real_push(delivery_harness, destination):
    h = delivery_harness
    h.config.destination = destination
    await push_delivery.GAME.execute(h.drive, h.event, "task_delivery")
    ordered = [("decision", "lamp", "cargo", "cargo"), ("tool", "go2_find_object"),
               ("decision", "lamp", "destination", "destination"),
               ("physical", "SET_COLOR"), ("tool", "go2_push_cargo")]
    assert [h.trace.index(step) for step in ordered] == sorted(h.trace.index(step) for step in ordered)
    assert [(name, params) for name, params in h.calls if name == "go2_push_cargo"] == [
        ("go2_push_cargo", {"cargo": "地上的小纸盒", "destination": destination, "distance_m": 0.15})]
    assert h.cargo_contexts == [{
        "reference": {"cargo": "地上的小纸盒", "destination": destination,
                      "cargo_location": h.cargo_location,
                      "search_evidence": h.search_evidence},
        "request_id": "delivery_msg_delivery_push_0:step", "signal_rgb": (255, 0, 0)}]
    assert not any(row[:3] == ("decision", "go2", "next") and row[-1] in {
        "waiting_destination", "delivered"} for row in h.trace)
    assert len([row for row in h.trace if row[:2] == ("brain_task", "go2")
                and row[-1] == "delivery_msg_delivery_push_0"]) == 1
    h.messenger.send_result.assert_called_once_with(
        "msg_delivery", "ok", task_id="task_delivery", reason="cargo_near_destination_confirmed")
    assert h.agent._peer_game_id is None


async def test_game_finishes_a_directional_attempt_without_claiming_delivery(delivery_harness):
    h = delivery_harness
    h.config.directional = True
    await push_delivery.GAME.execute(h.drive, h.event, 'task_delivery')
    assert not any(row[:3] == ('decision', 'go2', 'next') and row[-1] in {
        'directional_push_completed', 'delivered'} for row in h.trace)
    assert len([name for name, _ in h.calls if name == 'go2_push_cargo']) == 1
    h.messenger.send_result.assert_called_once_with(
        'msg_delivery', 'ok', task_id='task_delivery', reason='directional_push_attempt_completed')
    assert h.agent._peer_game_id is None


async def test_silent_delivery_steps_keep_calls_receipts_and_necessary_conversation(delivery_harness, monkeypatch):
    h = delivery_harness
    decide = push_delivery.decide

    async def silent_steps(role, phase, facts, **kwargs):
        answer = await decide(role, phase, facts, **kwargs)
        if phase == "agree" or phase == "next" and facts["stage"] == "delivery":
            answer.pop("speech", None)
        if phase == "next" and facts["stage"] == "delivery":
            answer.update(decision_reason="当前观察确认了本单货物和目的地。",
                          evidence_refs=[facts["observation"]["ref"]])
        return answer

    monkeypatch.setattr(push_delivery, "decide", silent_steps)
    await push_delivery.GAME.execute(h.drive, h.event, "task_delivery")
    messages = [event["text"] for event in h.events if event["event_type"] == "message"]
    assert messages == ["请先找地上的小纸盒。", "我找到货物了，这单送去哪里？",
                        "送到红色地垫附近，这单约定用红光表示。"]
    action = next(event for event in h.events if event.get("action") == "go2_push_cargo"
                  and event["event_type"] == "action")
    receipt = next(event for event in h.events if event.get("action") == "go2_push_cargo"
                   and event["event_type"] == "action_result")
    assert action["text"] == ""
    assert action["arguments"] == {"cargo": "地上的小纸盒", "destination": "红色地垫", "distance_m": .15}
    assert action["decision_basis"] == receipt["decision_basis"]
    assert action["decision_basis"]["reason"] not in messages
    assert action["decision_basis"]["evidence"][0]["ref"] in {
        event["event_id"] for event in h.events if event["event_type"] == "observation"}
    assert receipt["task_id"] == action["task_id"] + ":step"
    assert receipt["public_result"]["delivered"] is True


async def test_delivery_question_is_spoken_once_and_new_reply_is_preserved(delivery_harness, monkeypatch):
    h = delivery_harness
    decide = push_delivery.decide
    asked = False

    async def clarify_once(role, phase, facts, **kwargs):
        nonlocal asked
        if phase == "next" and facts["stage"] == "delivery" and not asked:
            asked = True
            return {"action": "question", "question": "指的是盒子后面的红垫吗？",
                    "speech": "我来复述一下我的疑问。"}
        if phase == "agree" and facts["stage"] == "clarification":
            return {"accepted": True, "speech": "对，就是你刚看到的那块红垫。"}
        return await decide(role, phase, facts, **kwargs)

    monkeypatch.setattr(push_delivery, "decide", clarify_once)
    await push_delivery.GAME.execute(h.drive, h.event, "task_delivery")
    messages = [event["text"] for event in h.events if event["event_type"] == "message"]
    assert messages.count("指的是盒子后面的红垫吗？") == 1
    assert "我来复述一下我的疑问。" not in messages
    assert "对，就是你刚看到的那块红垫。" in messages
    assert "同一货物已确认在指定标识附近，这单大致送到了。" not in messages
    assert len([name for name, _ in h.calls if name == "go2_push_cargo"]) == 1


async def test_optional_display_reference_cannot_abort_delivery(delivery_harness, monkeypatch):
    h = delivery_harness
    decide = push_delivery.decide

    async def malformed_explanation(role, phase, facts, **kwargs):
        answer = await decide(role, phase, facts, **kwargs)
        if phase == "next":
            answer.update(decision_reason="本单可以继续执行。",
                          evidence_refs=["cargo_receipt:unregistered"])
        return answer

    monkeypatch.setattr(push_delivery, "decide", malformed_explanation)
    await push_delivery.GAME.execute(h.drive, h.event, "task_delivery")
    h.messenger.send_result.assert_called_once_with(
        "msg_delivery", "ok", task_id="task_delivery", reason="cargo_near_destination_confirmed")
    assert len([name for name, _ in h.calls if name == "go2_push_cargo"]) == 1
    assert all(event.get("context_scope") == "process" for event in h.events
               if event["event_type"] in {"action", "action_result", "observation"})


@pytest.mark.parametrize('change', ['missing_stop', 'wrong_confirmation', 'unknown'])
def test_directional_completion_requires_the_actual_stop_receipt(change):
    receipt = {'result': 'ok', 'directional_push_attempted': True,
               'confirmation': 'directional_move_and_stop', 'stop': {'result': 'ok'}}
    if change == 'missing_stop':
        receipt.pop('stop')
    elif change == 'wrong_confirmation':
        receipt['confirmation'] = 'positioning_and_stop'
    else:
        receipt['result'] = 'unknown'
    assert push_delivery._push_completed(receipt) is False


@pytest.mark.parametrize("push_result", ["blocked", "unknown"])
async def test_unconfirmed_push_never_claims_delivery(delivery_harness, push_result):
    h = delivery_harness
    h.config.push_result = push_result
    await push_delivery.GAME.execute(h.drive, h.event, "task_delivery")
    assert ("decision", "go2", "next", "delivered") not in h.trace
    assert not any("这单送到了" in entry["text"] for entry in h.events)
    h.messenger.send_result.assert_called_once_with(
        "msg_delivery", "unknown", task_id="task_delivery", reason="cargo_push_unconfirmed:"+push_result)
    h.navigator.stop_and_confirm.assert_awaited_once()
    assert h.agent._peer_game_id is None


async def test_delivery_cleanup_keeps_trigger_separate_from_unknown_stop(delivery_harness):
    h = delivery_harness
    h.config.push_result = "blocked"
    h.navigator.stop_and_confirm.return_value = {"result": "accepted", "reason": "telemetry_stale"}
    outcome = await push_delivery.GAME.execute(h.drive, h.event, "task_delivery")
    assert outcome["result"] == "unknown"
    assert outcome["reason"] == "cargo_push_unconfirmed:blocked"
    assert outcome["physical_stop"] == "unknown"
    assert outcome["stop_reason"] == "telemetry_stale"
    assert outcome["stage"] == "push"
    h.messenger.send_result.assert_called_once_with("msg_delivery", "unknown",
        task_id="task_delivery", reason="cargo_push_unconfirmed:blocked")
    assert h.agent._peer_game_id is None


async def test_delivery_cancel_drains_child_and_journals_original_cause(delivery_harness, monkeypatch):
    h = delivery_harness
    module = sys.modules["cloud.peers.games.role_graph"]
    original = module.run_device_task
    ready, drained = asyncio.Event(), asyncio.Event()

    async def wait_for_cargo(role, goal, **kwargs):
        if kwargs["run_id"].endswith("_cargo"):
            ready.set()
            try:
                await asyncio.Future()
            finally:
                drained.set()
        return await original(role, goal, **kwargs)

    monkeypatch.setattr(module, "run_device_task", wait_for_cargo)
    h.navigator.stop_and_confirm.return_value = {"result": "accepted", "reason": "telemetry_stale"}
    task = asyncio.create_task(push_delivery.GAME.execute(h.drive, h.event, "task_delivery"))
    await asyncio.wait_for(ready.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained.is_set()
    assert h.agent._peer_game_id is None
    finish = h.drive._advance_task.await_args.kwargs["result"]
    assert finish["reason"] == "user_intervention"
    assert finish["physical_stop"] == "unknown"
    assert finish["stop_reason"] == "telemetry_stale"
    assert finish["stage"] == "find_cargo"
    assert not any(name == "go2_push_cargo" for name, _ in h.calls)
    h.messenger.send_result.assert_not_called()  # The game owner closes cancellation once.


async def test_scope_revocation_keeps_activity_intervention_cause(delivery_harness, monkeypatch):
    h = delivery_harness
    monkeypatch.setattr(push_delivery, "game_scope_available", lambda: False)
    outcome = await push_delivery.GAME.execute(h.drive, h.event, "task_delivery")
    assert outcome["reason"] == "user_intervention"
    assert outcome["physical_stop"] == "confirmed"
    assert not h.calls
    assert h.agent._peer_game_id is None
