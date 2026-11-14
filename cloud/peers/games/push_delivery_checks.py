"""Small mocked delivery checks: ordering and receipts, without real devices."""
from __future__ import annotations

import asyncio
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from cloud.peers.games import get_game, playable_games, push_delivery
from cloud.peers.games.delivery_decisions import DestinationDecision, NextDecision
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
    DestinationDecision.model_validate(valid)
    for invalid in ({**valid, "destination": " "}, {**valid, "evidence_refs": []},
                    {**valid, "evidence_refs": [" "]}, {**valid, "color": None},
                    {**valid, "color": {"r": 256, "g": 0, "b": 0, "brightness": 160}},
                    {**valid, "color": {"r": 0, "g": 0, "b": 0, "brightness": 160}}):
        with pytest.raises(ValidationError):
            DestinationDecision.model_validate(invalid)
    for distance in (0.09, 0.26, None):
        with pytest.raises(ValidationError):
            NextDecision(action="push", speech="尝试短距推送。", distance_m=distance)


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
    config = SimpleNamespace(push_result="ok")
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
        assert refs and all(ref in available for ref in refs)
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
            assert facts["courier_report"]["action"] == "question"
            return {"accepted": True, "destination": "红色地垫", "color_name": "红色",
                    "color": {"r": 255, "g": 0, "b": 0, "brightness": 160},
                    "evidence_refs": [facts["observation"]["ref"]],
                    "speech": "送到红色地垫，我用红光标示。"}
        assert role == "go2" and phase == "next"
        if stage == "waiting_destination":
            assert facts["destination_confirmed"] is False
            return {"action": "question", "question": "我找到货物了，这单送去哪里？",
                    "distance_m": None, "speech": "货物找到了，我等你指定目的地。"}
        assert facts["destination_confirmed"] is True
        if stage == "delivered":
            assert facts["latest_push"]["delivered"] is True
            assert facts["latest_push"]["confirmation"] == "cargo_contained_in_verified_zone_and_stopped"
            assert facts["latest_push"]["stop"]["result"] == "ok"
            return {"action": "finish", "question": "", "distance_m": None,
                    "speech": "同一货物已确认在指定区域里，这单送到了。"}
        assert stage == "delivery"
        return {"action": "push", "question": "", "distance_m": 0.15,
                "speech": "我沿已确认的方向短距推送这件货物。"}

    @contextmanager
    def search_request(request_id, _deadline):
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
            return {"result": "ok", "message": "地面有一个小纸盒，旁边有边界清楚的红色地垫。"}
        if name == "go2_find_object":
            return {"result": "ok", "outcome": "nearby", "found": True,
                    "target": params["target"], "task_id": search_ids[-1], "stop": {"result": "ok"},
                    "evidence": deepcopy(search_evidence)}
        assert name == "go2_push_cargo"
        return {"result": config.push_result, "delivered": config.push_result == "ok",
                "confirmation": "cargo_contained_in_verified_zone_and_stopped",
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

    stubs = {
        "cloud.api.intents": {"_dispatch_mqtt_task": Mock()},
        "cloud.cards.registry": {"get_registry": lambda: SimpleNamespace(get_card=lambda _uid: {})},
        "cloud.esp32.service": {"get_agent": lambda: agent},
        "cloud.esp32.tool_executor": {"execute_physical": execute_physical},
        "cloud.go2.connection": {"go2": SimpleNamespace(is_connected=True, _odom_session_id="delivery-odom")},
        "cloud.go2.control": {"control_lease": SimpleNamespace(snapshot=lambda: None)},
        "cloud.go2.navigation.navigator": {"navigator": navigator},
        "cloud.go2.navigation.cargo_push": {"expect_cargo": expect_cargo},
        "cloud.go2.tools.registry": {"get_tool_spec": lambda _name: object()},
        "cloud.go2.tools.executor": {"execute_tool": execute_tool},
        "cloud.space.activity": {"get_activity_store": lambda: SimpleNamespace(record_event=record)},
        "cloud.space.context": {"get_space_context": space_context},
        "cloud.space.decisions": {"resolve_decision": resolve_decision},
        "cloud.agents.go2.workflows.search": {"search_request": search_request},
        "cloud.peers.games.color_hunt": {"live_lamp_color": lambda _uid: deepcopy(lamp_color)},
    }
    for name, attributes in stubs.items():
        _stub_module(monkeypatch, name, **attributes)
    from cloud.peers.games import base
    monkeypatch.setattr(base, "activity_allows", lambda *_args: True)
    monkeypatch.setattr(push_delivery, "decide", decide)
    monkeypatch.setattr(push_delivery, "FileCheckpointSaver", _MemorySaver)
    monkeypatch.setattr(push_delivery, "invoke_blocking", invoke_blocking)
    event = {"message_id": "msg_delivery", "messenger": messenger,
             "activity_id": "act_delivery", "deadline": time.time()+60,
             "message": {"sender": push_delivery.LAMP, "content": "来玩指挥官与送货员。"},
             "acceptance_message_id": "msg_delivery_acceptance"}
    return SimpleNamespace(config=config, event=event, drive=drive, messenger=messenger,
                           agent=agent, navigator=navigator, calls=calls, events=events, trace=trace,
                           cargo_contexts=cargo_contexts, search_evidence=search_evidence)


async def test_delivery_orders_find_report_destination_signal_and_real_push(delivery_harness):
    h = delivery_harness
    await push_delivery.GAME.execute(h.drive, h.event, "task_delivery")
    ordered = [("decision", "lamp", "cargo", "cargo"), ("tool", "go2_find_object"),
               ("decision", "go2", "next", "waiting_destination"),
               ("decision", "lamp", "destination", "destination"),
               ("physical", "SET_COLOR"), ("tool", "go2_push_cargo"),
               ("decision", "go2", "next", "delivered")]
    assert [h.trace.index(step) for step in ordered] == sorted(h.trace.index(step) for step in ordered)
    assert [(name, params) for name, params in h.calls if name == "go2_push_cargo"] == [
        ("go2_push_cargo", {"cargo": "地上的小纸盒", "destination": "红色地垫", "distance_m": 0.15})]
    assert h.cargo_contexts == [{
        "reference": {"cargo": "地上的小纸盒", "destination": "红色地垫",
                      "search_evidence": h.search_evidence},
        "request_id": "delivery_msg_delivery_push_0", "signal_rgb": (255, 0, 0)}]
    h.messenger.send_result.assert_called_once_with(
        "msg_delivery", "ok", task_id="task_delivery", reason="cargo_in_zone_confirmed")
    assert h.agent._peer_game_id is None


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
