"""Small mocked game-flow checks; no models, network or hardware are used."""
from __future__ import annotations

import asyncio
import sys
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cloud.peers.games import rps_siege
from cloud.peers.games.rps_decisions import Go2Setup, PerformanceDecision
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
    calls, timeline, decisions, observations = [], [], [], []
    target_location = {"x": 1.5, "y": 0.2, "odom_session_id": "test-odom"}
    fixture = SimpleNamespace(
        lamp_choices=["scissors"], dog_choices=["rock"],
        gesture_result="ok", performance_result="ok", physical_result="ok", off_result="ok",
        observation={"result": "ok", "message": "桌上的智能灯清楚可见。"}, target_visible=True,
        agreement=True, interrupt_after=None, interrupt_after_off=False,
        physical_results=[],
        navigation_results=[
            {"result": "ok", "confirmation": "odom_stopped_and_visual_recheck",
             "target_location": target_location},
        ],
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

    def end(owner, *, preserve_output=False):
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
        if phase == "identify":
            assert role == "lamp"
            return {"color": {"r": 0, "g": 100, "b": 255, "brightness": 160},
                    "speech": "我是亮蓝色的圆球灯，请看看我在哪里。"}
        if phase == "setup":
            return {"speech": "我选好这局的表达了。",
                    **({"target": "桌上的智能灯" if fixture.target_visible else None,
                        "target_visible": fixture.target_visible} if role == "go2" else {}),
                    "mapping": ({
                        "rock": {"r": 255, "g": 0, "b": 0, "brightness": 160},
                        "scissors": {"r": 0, "g": 255, "b": 0, "brightness": 160},
                        "paper": {"r": 0, "g": 0, "b": 255, "brightness": 160},
                    } if role == "lamp" else {
                        "rock": "Hello", "scissors": "Stretch", "paper": "Content"})}
        if phase == "agree":
            return {"accepted": fixture.agreement, "speech": "同意这局约定。"}
        if phase == "performance":
            return {"command": "Dance2", "speech": "这把你赢了，我来跳个舞。"}
        assert phase == "throw"
        assert "choices" not in facts  # Never send a current private choice to either model.
        choices = fixture.lamp_choices if role == "lamp" else fixture.dog_choices
        return {"choice": choices[facts["round"]-1], "speech": "我选好了。"}

    async def execute_tool(name, params, **kwargs):
        calls.append((name, deepcopy(params)))
        if fixture.interrupt_after == name:
            drive.user_interrupt = True
        if name == "go2_observe":
            observations.append(deepcopy(kwargs))
            return fixture.observation
        if name == "go2_sport":
            return {"result": fixture.performance_result if kwargs.get("request_id", "").endswith("_performance")
                    else fixture.gesture_result}
        assert name == "go2_navigate_to_visible_target"
        return fixture.navigation_results.pop(0)

    def execute_physical(**kwargs):
        calls.append((kwargs["action"], deepcopy(kwargs["params"])))
        off = kwargs["action"] == "SET_STATE" and kwargs["params"] == {"state": "OFF"}
        if off and fixture.interrupt_after_off:
            drive.user_interrupt = True
        result = (fixture.physical_results.pop(0) if fixture.physical_results
                  else fixture.off_result if off else fixture.physical_result)
        return {"result": result, "task_id": kwargs["task_id"]}

    async def invoke_blocking(callback, *args, **kwargs):
        return callback(*args, **kwargs)

    store = SimpleNamespace(record_event=lambda _activity, kind, **kwargs:
                            timeline.append({"event_type": kind, **kwargs}))
    stubs = {
        "cloud.cards.registry": {"get_registry": lambda: SimpleNamespace(get_card=lambda _unit: {})},
        "cloud.esp32.service": {"get_agent": lambda: agent},
        "cloud.esp32.tool_executor": {"execute_physical": execute_physical},
        "cloud.go2.connection": {"go2": SimpleNamespace(is_connected=True)},
        "cloud.go2.navigation.navigator": {"navigator": navigator},
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
                           timeline=timeline, decisions=decisions,
                           observations=observations,
                           target_location=target_location)


async def test_dog_wins_once_then_full_approach_and_confirmed_switch_off(game_harness):
    h = game_harness
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    advances = [params for name, params in h.calls if name == "go2_navigate_to_visible_target"]
    assert advances == [{"target": "桌上的智能灯"}]
    off_indices = [index for index, call in enumerate(h.calls)
                   if call == ("SET_STATE", {"state": "OFF"})]
    assert len(off_indices) == 1 and off_indices[0] == len(h.calls)-1
    h.messenger.send_result.assert_called_once_with(
        "msg_test_rps", "ok", task_id="task_test_rps", reason="go2_won_lamp_off_confirmed")
    h.messenger.get_status.assert_called_with("msg_test_acceptance")
    assert len([1 for _role, phase, _facts in h.decisions if phase == "throw"]) == 2
    h.agent.end_peer_game.assert_called_once_with("rps:msg_test_rps", preserve_output=True)
    finish = h.drive._advance_task.await_args.kwargs
    assert finish["stage"] == "finish" and finish["result"]["game_winner"] == "go2"
    assert h.agent._peer_game_id is None


async def test_spherical_lamp_announces_confirmed_color_before_fresh_observation(game_harness):
    h = game_harness
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    color = {"r": 0, "g": 100, "b": 255, "brightness": 160}
    assert h.calls[:2] == [("SET_STATE", {"state": "ON"}), ("SET_COLOR", color)]
    assert h.calls[2][0] == "go2_observe"
    question = h.calls[2][1]["question"]
    assert "圆球灯" in question and "亮蓝色" in question and str(color) in question
    assert "多个无法区分" in question and "不依赖当前颜色" in question
    cue = next(facts["lamp_identification"] for role, phase, facts in h.decisions
               if role == "go2" and phase == "setup")
    assert cue["color"] == color
    assert h.observations[0]["observation_after"] == cue["confirmed_at"]
    announcement = next(i for i, row in enumerate(h.timeline) if row.get("stage") == "identification")
    assert all(row["event_type"] == "action_result" and row["status"] == "ok"
               for row in h.timeline[:announcement])
    assert h.timeline[announcement + 1]["event_type"] == "observation"


@pytest.mark.parametrize("receipts", [["accepted"], ["ok", "timeout"]])
async def test_unconfirmed_identification_cannot_announce_or_observe(game_harness, receipts):
    h = game_harness
    h.config.physical_results = receipts
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(name == "go2_observe" for name, _params in h.calls)
    assert not any(row.get("stage") == "identification" for row in h.timeline)
    assert [phase for _role, phase, _facts in h.decisions] == ["identify"]
    assert h.drive._advance_task.await_args.kwargs["result"]["game_winner"] is None


async def test_lamp_wins_then_model_selected_performance_without_navigation(game_harness):
    h = game_harness
    h.config.lamp_choices = ["paper"]
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(name == "go2_navigate_to_visible_target" for name, _params in h.calls)
    assert ("SET_STATE", {"state": "OFF"}) not in h.calls
    h.messenger.send_result.assert_called_once_with(
        "msg_test_rps", "ok", task_id="task_test_rps", reason="lamp_won_performance_confirmed")
    assert [params for name, params in h.calls if name == "go2_sport"] == [{"cmd": "Hello"}, {"cmd": "Dance2"}]
    performance = next(facts for role, phase, facts in h.decisions if phase == "performance")
    assert performance["winner"] == "lamp"
    h.agent.end_peer_game.assert_called_once_with("rps:msg_test_rps", preserve_output=False)


async def test_draw_repeats_without_moving_until_first_decisive_throw(game_harness):
    h = game_harness
    # Enough draws to exceed the old graph recursion limit, without a quota.
    h.config.lamp_choices = ["rock"] * 35 + ["scissors"]
    h.config.dog_choices = ["rock"] * 36
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert len([1 for name, _ in h.calls if name == "go2_sport"]) == 36
    assert [params for name, params in h.calls if name == "go2_navigate_to_visible_target"] == [{"target": "桌上的智能灯"}]
    last_throw = [facts for _role, phase, facts in h.decisions if phase == "throw"][-1]
    assert last_throw["round"] == 36 and all(row["winner"] == "draw" for row in last_throw["history"])
    h.messenger.send_result.assert_called_once_with(
        "msg_test_rps", "ok", task_id="task_test_rps", reason="go2_won_lamp_off_confirmed")


@pytest.mark.parametrize("reason", ["lamp_agent_unavailable", "lamp_busy"])
async def test_preflight_failure_completes_announced_task(game_harness, monkeypatch, reason):
    h = game_harness
    if reason == "lamp_agent_unavailable":
        monkeypatch.setattr(sys.modules["cloud.esp32.service"], "get_agent", lambda: None)
    elif reason == "lamp_busy":
        h.agent.begin_peer_game = Mock(return_value=False)
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
    assert not any(event.get("stage") == "rps_result" for event in h.timeline)
    h.messenger.send_result.assert_called_once_with(
        "msg_test_rps", "unknown", task_id="task_test_rps",
        reason="go2_gesture_unconfirmed:unknown")
    h.navigator.stop_and_confirm.assert_awaited_once()


@pytest.mark.parametrize("observation", [{"result": "error", "message": "无法观察"},
                                         {"result": "ok", "message": "  "}])
async def test_failed_or_empty_observation_stops_before_setup(game_harness, observation):
    h = game_harness
    h.config.observation = observation
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert [phase for _role, phase, _facts in h.decisions] == ["identify"]
    assert [name for name, _ in h.calls] == ["SET_STATE", "SET_COLOR", "go2_observe"]
    assert h.messenger.send_result.call_args.kwargs["reason"] == "game_observation_unavailable"


async def test_unidentifiable_lamp_rejects_before_agreement_or_throw(game_harness):
    h = game_harness
    h.config.target_visible = False
    h.config.observation = {"result": "ok", "message": "画面中没有可辨认的灯或固定标记。"}
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert {phase for _role, phase, _facts in h.decisions} == {"identify", "setup"}
    assert [name for name, _ in h.calls] == ["SET_STATE", "SET_COLOR", "go2_observe"]
    h.messenger.send_result.assert_called_once_with("msg_test_rps", "rejected", task_id="task_test_rps",
                                                   reason="game_target_not_visible")


async def test_disagreement_stops_before_throw(game_harness):
    h = game_harness
    h.config.agreement = False
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(phase == "throw" for _role, phase, _facts in h.decisions)
    assert h.messenger.send_result.call_args.kwargs["reason"] == "game_terms_not_agreed"


@pytest.mark.parametrize("confirmation", [None, "odom_stopped_and_visual_progress"])
async def test_unconfirmed_arrival_keeps_dog_win_but_never_switches_off(game_harness, confirmation):
    h = game_harness
    h.config.navigation_results = [{"result": "ok", "confirmation": confirmation}]
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert ("SET_STATE", {"state": "OFF"}) not in h.calls
    assert h.messenger.send_result.call_args.kwargs["reason"] == "go2_won:game_approach_unconfirmed:ok"
    assert any(row.get("stage") == "rps_result" and row.get("reason") == "go2_won" for row in h.timeline)
    assert "狗获胜" in h.timeline[-1]["text"] and "未确认完成" in h.timeline[-1]["text"]


async def test_navigation_failure_does_not_change_winner(game_harness):
    h = game_harness
    h.navigator.snapshot = lambda: (None, "map_unavailable")
    h.config.navigation_results = [{"result": "blocked", "reason": "map_unavailable"}]
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert h.messenger.send_result.call_args.kwargs["reason"] == "go2_won:game_approach_unconfirmed:map_unavailable"
    assert h.drive._advance_task.await_args.kwargs["result"]["game_winner"] == "go2"
    h.navigator.stop_and_confirm.assert_awaited_once()


async def test_lamp_win_can_complete_without_a_navigation_map(game_harness):
    h = game_harness
    h.navigator.snapshot = lambda: (None, "map_unavailable")
    h.config.lamp_choices = ["paper"]
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    h.messenger.send_result.assert_called_once_with(
        "msg_test_rps", "ok", task_id="task_test_rps", reason="lamp_won_performance_confirmed")


async def test_unconfirmed_switch_off_preserves_dog_win_without_preserving_output(game_harness):
    h = game_harness
    h.config.off_result = "timeout"
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert h.messenger.send_result.call_args.kwargs["reason"] == "go2_won:lamp_action_unconfirmed:timeout"
    h.agent.end_peer_game.assert_called_once_with("rps:msg_test_rps", preserve_output=False)


async def test_unconfirmed_performance_preserves_lamp_win(game_harness):
    h = game_harness
    h.config.lamp_choices = ["paper"]
    h.config.performance_result = "accepted"
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert h.messenger.send_result.call_args.kwargs["reason"] == "lamp_won:game_performance_unconfirmed:accepted"
    assert h.drive._advance_task.await_args.kwargs["result"]["game_winner"] == "lamp"
    assert "灯获胜" in h.timeline[-1]["text"] and "未确认完成" in h.timeline[-1]["text"]


async def test_user_interruption_after_arrival_prevents_switch_off(game_harness):
    h = game_harness
    h.config.interrupt_after = "go2_navigate_to_visible_target"
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert ("SET_STATE", {"state": "OFF"}) not in h.calls
    assert h.messenger.send_result.call_args.kwargs["reason"] == "go2_won:autonomy_interrupted_or_offline"
    h.agent.end_peer_game.assert_called_once_with("rps:msg_test_rps", preserve_output=False)


async def test_confirmed_off_receipt_survives_immediate_user_interruption(game_harness):
    h = game_harness
    h.config.interrupt_after_off = True
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert h.messenger.send_result.call_args.kwargs["reason"] == "go2_won:autonomy_interrupted_or_offline"
    h.agent.end_peer_game.assert_called_once_with("rps:msg_test_rps", preserve_output=True)


async def test_expired_game_cannot_observe_or_act(game_harness):
    h = game_harness
    h.event["deadline"] = time.time() - 1
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not h.calls and not h.decisions
    assert h.messenger.send_result.call_args.kwargs["reason"] == "game_expired"


async def test_unconfirmed_lamp_display_cannot_score(game_harness):
    h = game_harness
    h.config.physical_result = "accepted"
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(name == "go2_sport" for name, _params in h.calls)
    assert not any(event.get("stage") == "rps_result" for event in h.timeline)
    assert h.messenger.send_result.call_args.kwargs["reason"] == "lamp_action_unconfirmed:accepted"


def test_target_decision_requires_visible_description_and_rejects_invented_hidden_target():
    setup = {"mapping": {"rock": "Hello", "scissors": "Stretch", "paper": "Content"},
             "speech": "看不到灯，先不开局。", "target_visible": False, "target": None}
    assert Go2Setup.model_validate(setup).target is None
    with pytest.raises(ValueError, match="unobserved_game_target"):
        Go2Setup.model_validate({**setup, "target": "猜测的灯"})
    with pytest.raises(ValueError, match="visible_game_target_required"):
        Go2Setup.model_validate({**setup, "target_visible": True})
    assert Go2Setup.model_validate({**setup, "target_visible": True, "target": " 桌上的灯 "}).target == "桌上的灯"


def test_performance_requires_supported_action_with_confirmable_standing_completion():
    assert PerformanceDecision(command="Dance1", speech="我来跳舞。").command == "Dance1"
    for command in ("InventedDance", "Move", "StandDown", "StopMove"):
        with pytest.raises(ValueError, match="game_performance_not_supported"):
            PerformanceDecision(command=command, speech="我来表演。")


def test_round_winner_covers_every_pair_and_rejects_unknown_choice():
    dog_wins = {("scissors", "rock"), ("paper", "scissors"), ("rock", "paper")}
    for lamp in rps_siege.CHOICES:
        for dog in rps_siege.CHOICES:
            expected = "draw" if lamp == dog else "go2" if (lamp, dog) in dog_wins else "lamp"
            assert rps_siege.round_winner(lamp, dog) == expected
    with pytest.raises(ValueError, match="invalid_rps_choice"):
        rps_siege.round_winner("unknown", "rock")
