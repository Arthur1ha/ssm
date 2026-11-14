"""Small mocked game-flow checks; no models, network or hardware are used."""
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

from cloud.peers.games import rps_siege
from cloud.peers.messaging import AgentMessenger
from cloud.space.execution import ExecutionContext


class _MemorySaver(InMemorySaver):
    @asynccontextmanager
    async def async_run_lock(self, _thread_id):
        yield


@pytest.fixture
async def game_harness(monkeypatch):
    """Stub device/service imports before entering the real coordinator graph."""
    loop = asyncio.get_running_loop()

    def inline_executor(_executor, callback, *args):
        # All external work is stubbed below. Run the graph's tiny synchronous
        # routing/state callbacks inline, keeping this mocked check thread-free.
        future = loop.create_future()
        try:
            future.set_result(callback(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    monkeypatch.setattr(loop, "run_in_executor", inline_executor)
    calls, timeline, decisions, anchors = [], [], [], []
    target_location = {"x": 1.5, "y": 0.2, "odom_session_id": "test-odom"}
    fixture = SimpleNamespace(
        lamp_choices=["scissors", "scissors"], dog_choices=["rock", "rock"],
        gesture_result="ok", navigation_results=[
            {"result": "ok", "confirmation": "odom_stopped_and_visual_progress", "arrived": False,
             "target_location": target_location},
            {"result": "ok", "confirmation": "odom_stopped_and_visual_recheck", "arrived": True,
             "target_location": target_location},
        ], max_rounds=2,
    )
    messenger = Mock()
    messenger.get_status.return_value = {"state": "ok"}
    agent = SimpleNamespace(
        _autonomy_epoch=1, _command_epoch=1, _peer_game_id=None,
        _should_act=lambda: True, _device_online=lambda: True,
        _state=SimpleNamespace(actuator_snapshot=lambda: {
            rps_siege.LAMP: {"state": {"ism": "ON"}}}),
    )

    def begin(owner):
        agent._peer_game_id = owner
        return True

    def end(owner):
        assert agent._peer_game_id == owner
        agent._peer_game_id = None

    agent.begin_peer_game = Mock(side_effect=begin)
    agent.end_peer_game = Mock(side_effect=end)
    navigator = SimpleNamespace(
        snapshot=lambda: (({"session_id": "test-odom"}, object()), None),
        stop_and_confirm=AsyncMock(return_value={"result": "ok"}),
    )
    drive = SimpleNamespace(
        _context=ExecutionContext("peer_test_rps", "visitor", False,
                                  origin_kind="agent_autonomous", expires_at=int(time.time()+60)),
        _activity_id=lambda: "act_test_rps", user_interrupt=False,
        _active_goal={"task_id": "peer_test_rps"},
        _advance_task=AsyncMock(return_value={"result": {"result": "ok"}}),
    )

    async def decide(role, phase, facts, **_kwargs):
        decisions.append((role, phase, deepcopy(facts)))
        if phase == "setup":
            return {"max_rounds": fixture.max_rounds, "advance_m": 0.3,
                    "target": "桌上的智能灯", "speech": "我选好这局的表达了。",
                    "mapping": ({
                        "rock": {"r": 255, "g": 0, "b": 0, "brightness": 160},
                        "scissors": {"r": 0, "g": 255, "b": 0, "brightness": 160},
                        "paper": {"r": 0, "g": 0, "b": 255, "brightness": 160},
                    } if role == "lamp" else {
                        "rock": "Hello", "scissors": "Stretch", "paper": "Content"})}
        if phase == "agree":
            return {"accepted": True, "speech": "同意这局约定。"}
        assert phase == "throw"
        assert "choices" not in facts  # Never send a current private choice to either model.
        choices = fixture.lamp_choices if role == "lamp" else fixture.dog_choices
        return {"choice": choices[facts["round"]-1], "speech": "我选好了。"}

    async def execute_tool(name, params, **kwargs):
        calls.append((name, deepcopy(params)))
        if name == "go2_observe":
            return {"result": "ok", "message": "桌上的智能灯清楚可见。"}
        if name == "go2_sport":
            return {"result": fixture.gesture_result}
        assert name == "go2_navigate_to_visible_target"
        return fixture.navigation_results.pop(0)

    def execute_physical(**kwargs):
        calls.append((kwargs["action"], deepcopy(kwargs["params"])))
        return {"result": "ok", "task_id": kwargs["task_id"]}

    async def invoke_blocking(callback, *args, **kwargs):
        return callback(*args, **kwargs)

    @contextmanager
    def expect_visual_target(anchor):
        anchors.append(deepcopy(anchor))
        yield

    store = SimpleNamespace(record_event=lambda _activity, kind, **kwargs:
                            timeline.append({"event_type": kind, **kwargs}))
    stubs = {
        "cloud.cards.registry": {"get_registry": lambda: SimpleNamespace(get_card=lambda _unit: {})},
        "cloud.esp32.service": {"get_agent": lambda: agent},
        "cloud.esp32.tool_executor": {"execute_physical": execute_physical},
        "cloud.go2.connection": {"go2": SimpleNamespace(is_connected=True)},
        "cloud.go2.navigation.navigator": {"navigator": navigator},
        "cloud.go2.navigation.target_approach": {"expect_visual_target": expect_visual_target},
        "cloud.go2.control": {"control_lease": SimpleNamespace(snapshot=lambda: None)},
        "cloud.go2.tools.executor": {"execute_tool": execute_tool},
        "cloud.space.activity": {"get_activity_store": lambda: store},
        "cloud.space.context": {"get_space_context": lambda *_args, **_kwargs: {}},
        "cloud.api.intents": {"_dispatch_mqtt_task": Mock()},
    }
    for name, attributes in stubs.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(rps_siege, "decide", decide)
    monkeypatch.setattr(rps_siege, "invoke_blocking", invoke_blocking)
    monkeypatch.setattr(rps_siege, "FileCheckpointSaver", _MemorySaver)
    monkeypatch.setattr(rps_siege, "activity_allows", lambda *_args: True)
    event = {"message_id": "msg_test_rps", "messenger": messenger,
             "activity_id": "act_test_rps", "deadline": time.time()+60,
             "message": {"sender": rps_siege.LAMP, "content": "来玩猜拳攻灯。"},
             "acceptance_message_id": "msg_test_acceptance"}
    return SimpleNamespace(config=fixture, drive=drive, event=event, agent=agent,
                           messenger=messenger, navigator=navigator, calls=calls,
                           timeline=timeline, decisions=decisions, anchors=anchors,
                           target_location=target_location)


async def test_dog_wins_advance_in_segments_then_confirmed_arrival_switches_off(game_harness):
    h = game_harness
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    advances = [params for name, params in h.calls if name == "go2_navigate_to_visible_target"]
    assert advances == [{"target": "桌上的智能灯", "max_advance_m": 0.3}] * 2
    assert h.anchors == [None, h.target_location]
    off_indices = [index for index, call in enumerate(h.calls)
                   if call == ("SET_STATE", {"state": "OFF"})]
    assert len(off_indices) == 1 and off_indices[0] == len(h.calls)-1
    h.messenger.send_result.assert_called_once_with(
        "msg_test_rps", "ok", task_id="task_test_rps", reason="go2_won_lamp_off_confirmed")
    h.messenger.get_status.assert_called_with("msg_test_acceptance")
    second_round = [facts for _role, phase, facts in h.decisions
                    if phase == "throw" and facts["round"] == 2]
    assert all(facts["history"][0]["winner"] == "go2" for facts in second_round)
    assert h.agent._peer_game_id is None


async def test_lamp_defends_without_navigation_or_switch_off(game_harness):
    h = game_harness
    h.config.lamp_choices = ["paper", "paper"]
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(name == "go2_navigate_to_visible_target" for name, _params in h.calls)
    assert ("SET_STATE", {"state": "OFF"}) not in h.calls
    h.messenger.send_result.assert_called_once_with(
        "msg_test_rps", "ok", task_id="task_test_rps", reason="lamp_defended")


@pytest.mark.parametrize("reason", ["lamp_agent_unavailable", "map_unavailable",
                                    "localization_unavailable", "lamp_busy"])
async def test_preflight_failure_completes_announced_task(game_harness, monkeypatch, reason):
    h = game_harness
    if reason == "lamp_agent_unavailable":
        monkeypatch.setattr(sys.modules["cloud.esp32.service"], "get_agent", lambda: None)
    elif reason == "lamp_busy":
        h.agent.begin_peer_game = Mock(return_value=False)
    else:
        h.navigator.snapshot = lambda: (None, reason)

    # Exercise the real progress/result validator. A plain Mock hid the
    # missing-task-ID rejection that left live invitations waiting forever.
    transport = AgentMessenger(None, None, None, set(), lambda _message: None)
    transport._publish = Mock(return_value=True)
    transport._inbound[h.event["message_id"]] = {
        "sender": rps_siege.LAMP, "target": "go2", "deadline": h.event["deadline"],
        "conversation_id": h.event["message_id"],
    }
    try:
        transport.send_progress(h.event["message_id"], "task_test_rps")
        h.messenger.send_result.side_effect = transport.send_result
        await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
        payload = transport._publish.call_args.args[0]
        assert payload["kind"] == "result" and payload["result"] == "blocked"
        assert payload["task_id"] == "task_test_rps" and payload["reason"] == reason
        assert payload["message_id"] == h.event["message_id"]
    finally:
        transport.close()
    assert not h.calls and not h.decisions
    h.drive._advance_task.assert_not_awaited()
    assert h.agent._peer_game_id is None
    assert any(entry["event_type"] == "action_result" and entry["status"] == "blocked"
               and entry["task_id"] == "task_test_rps" and "无法开局" in entry["text"]
               for entry in h.timeline)


async def test_unconfirmed_gesture_cannot_score_advance_or_win(game_harness):
    h = game_harness
    h.config.gesture_result = "unknown"
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(name == "go2_navigate_to_visible_target" for name, _params in h.calls)
    assert ("SET_STATE", {"state": "OFF"}) not in h.calls
    assert not any("获得一次推进机会" in event["text"] or "狗获胜" in event["text"]
                   or "灯守擂成功" in event["text"] for event in h.timeline)
    h.messenger.send_result.assert_called_once_with(
        "msg_test_rps", "unknown", task_id="task_test_rps",
        reason="go2_gesture_unconfirmed:unknown")
    h.navigator.stop_and_confirm.assert_awaited_once()


def test_round_winner_covers_every_pair_and_rejects_unknown_choice():
    dog_wins = {("scissors", "rock"), ("paper", "scissors"), ("rock", "paper")}
    for lamp in rps_siege.CHOICES:
        for dog in rps_siege.CHOICES:
            expected = "draw" if lamp == dog else "go2" if (lamp, dog) in dog_wins else "lamp"
            assert rps_siege.round_winner(lamp, dog) == expected
    with pytest.raises(ValueError, match="invalid_rps_choice"):
        rps_siege.round_winner("unknown", "rock")
