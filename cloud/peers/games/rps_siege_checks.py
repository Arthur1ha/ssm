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
from cloud.peers.games.rps_decisions import Go2Identification, LampSetup, PerformanceDecision
from cloud.peers.messaging import AgentMessenger
from cloud.space.execution import ExecutionContext


class _MemorySaver(InMemorySaver):
    @asynccontextmanager
    async def async_run_lock(self, _thread_id):
        yield


@pytest.mark.parametrize("accepted", [True, False])
def test_dog_invitation_asks_lamp_willingness_before_any_identification(monkeypatch, tmp_path, accepted):
    from cloud.peers.games import rps_decisions, rps_lifecycle

    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    monkeypatch.setattr(rps_lifecycle, "game_invitation_ready", lambda *_: True)
    agent = SimpleNamespace(begin_peer_game=Mock())
    for name, attributes in {
        "cloud.esp32.service": {"get_agent": lambda: agent},
        "cloud.space.context": {"get_space_context": lambda *_args, **_kwargs: {
            "lamp_state": "OFF", "target_identified": False}},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    agent._state = None
    decide = AsyncMock(return_value={"accepted": accepted, "speech": "我来参与准备。" if accepted else "这次先不玩。"})
    monkeypatch.setattr(rps_decisions, "decide", decide)
    messenger = Mock()
    messenger.send_message.return_value = {"result": "accepted", "message_id": "lamp_reply"}
    invitation = {"kind": "message", "interaction_kind": "game_invite", "game_id": "rps_siege",
                  "activity_id": "act_test", "message_id": "dog_invite", "sender": "go2",
                  "target": rps_siege.LAMP, "content": "来玩猜拳？", "deadline": time.time() + 60}
    rps_lifecycle.on_invitation(messenger, invitation, None)
    assert decide.await_args.args[:2] == ("lamp", "invite")
    assert "接受参与→公开双方映射→确认规则→认灯定位→秘密选拳并同时展示" in decide.await_args.args[2]["game"]["rules"]
    assert messenger.send_message.call_args.kwargs["reply_kind"] == ("accepted" if accepted else "declined")
    assert messenger.send_message.call_args.kwargs["reply_to"] == "dog_invite"
    agent.begin_peer_game.assert_not_called()
    if accepted:
        messenger.send_result.assert_not_called()
    else:
        messenger.send_result.assert_called_once_with("dog_invite", "rejected", reason="invitation_declined")


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
    fixture = SimpleNamespace(
        lamp_choices=["scissors"], dog_choices=["rock"],
        gesture_result="ok", switch_off_gesture_result="ok", performance_result="ok", physical_result="ok", off_result="ok",
        observation={"result": "ok", "message": "桌上的智能灯清楚可见。"}, target_visible=True,
        agreement=True, interrupt_after=None, interrupt_after_off=False,
        physical_results=[], interrupt_decision=None, concurrent_reveal=False,
        search_results=[
            {"result": "ok", "outcome": "nearby", "found": True,
             "target": "桌上的智能灯", "task_id": "rps_msg_test_rps_lamp",
             "stop": {"result": "ok"}},
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
        if fixture.interrupt_decision == (role, phase):
            drive.user_interrupt = True
        if phase == "identify":
            if role == "go2":
                return {"target": "桌上的智能灯" if fixture.target_visible else None,
                        "target_visible": fixture.target_visible,
                        "speech": "认出了桌上的圆球灯。" if fixture.target_visible else "没能认出灯。"}
            return {"color": {"r": 0, "g": 100, "b": 255, "brightness": 160},
                    "speech": "我是亮蓝色的圆球灯，请看看我在哪里。"}
        if phase == "setup":
            return {"speech": "我选好这局的表达了。",
                    **({"color_names": {"rock": "红色", "scissors": "绿色", "paper": "蓝色"}}
                       if role == "lamp" else {}),
                    "mapping": ({
                        "rock": {"r": 255, "g": 0, "b": 0, "brightness": 160},
                        "scissors": {"r": 0, "g": 255, "b": 0, "brightness": 160},
                        "paper": {"r": 0, "g": 0, "b": 255, "brightness": 160},
                    } if role == "lamp" else {
                        "rock": "Hello", "scissors": "Stretch", "paper": "Content"})}
        if phase == "agree":
            # Both actual mappings are public before agreement; static game
            # instructions belong to model context, not the progress timeline.
            assert len([row for row in timeline if row.get("stage") == "mapping_proposal"]) == 2
            assert not any("rules" in row.get("public_result", {}).get("game_rules", {})
                           for row in timeline)
            return {"accepted": fixture.agreement, "speech": "同意这局约定。"}
        if phase == "performance":
            return {"command": "Dance2", "speech": "这把你赢了，我来跳个舞。"}
        assert phase == "throw"
        assert "choices" not in facts  # Never send a current private choice to either model.
        choices = fixture.lamp_choices if role == "lamp" else fixture.dog_choices
        return {"choice": choices[facts["round"]-1], "speech": "我选好了。"}

    searches = []
    active_search = None
    reveal_started = {role: asyncio.Event() for role in ("lamp", "go2")}
    reveal_release = {role: asyncio.Event() for role in ("lamp", "go2")}
    for release in reveal_release.values():
        release.set()
    switch_off_started, switch_off_release = asyncio.Event(), asyncio.Event()
    switch_off_release.set()

    @contextmanager
    def search_request(request_id, deadline):
        nonlocal active_search
        active_search = (request_id, deadline)
        searches.append(active_search)
        try:
            yield
        finally:
            active_search = None

    async def execute_tool(name, params, **kwargs):
        calls.append((name, deepcopy(params)))
        if fixture.interrupt_after == name:
            drive.user_interrupt = True
        if name == "go2_observe":
            observations.append(deepcopy(kwargs))
            return fixture.observation
        if name == "go2_sport":
            if kwargs.get("request_id", "").endswith("_switch_off_gesture"):
                assert kwargs["request_id"] == "rps_msg_test_rps_switch_off_gesture"
                assert kwargs["context"].session_id == drive._context.session_id
                switch_off_started.set()
                await switch_off_release.wait()
                return {"result": fixture.switch_off_gesture_result,
                        "command_request_id": kwargs["request_id"]}
            if fixture.concurrent_reveal and kwargs.get("request_id", "").endswith("_gesture"):
                reveal_started["go2"].set()
                await asyncio.wait_for(reveal_started["lamp"].wait(), timeout=1)
                await reveal_release["go2"].wait()
            return {"result": fixture.performance_result if kwargs.get("request_id", "").endswith("_performance")
                    else fixture.gesture_result}
        assert name == "go2_find_object"
        assert active_search is not None
        assert active_search[0] == "rps_msg_test_rps_lamp"
        assert int(active_search[1]) == kwargs["context"].expires_at
        return fixture.search_results.pop(0)

    def execute_physical(**kwargs):
        calls.append((kwargs["action"], deepcopy(kwargs["params"])))
        off = kwargs["action"] == "SET_STATE" and kwargs["params"] == {"state": "OFF"}
        if off and fixture.interrupt_after_off:
            drive.user_interrupt = True
        result = (fixture.physical_results.pop(0) if fixture.physical_results
                  else fixture.off_result if off else fixture.physical_result)
        return {"result": result, "task_id": kwargs["task_id"]}

    async def invoke_blocking(callback, *args, **kwargs):
        result = callback(*args, **kwargs)
        if (fixture.concurrent_reveal and kwargs.get("action") == "SET_COLOR"
                and kwargs["params"] != {"r": 0, "g": 100, "b": 255, "brightness": 160}):
            reveal_started["lamp"].set()
            await asyncio.wait_for(reveal_started["go2"].wait(), timeout=1)
            await reveal_release["lamp"].wait()
        return result

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
        "cloud.agents.go2.workflows.search": {"search_request": search_request},
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
                           searches=searches, reveal_started=reveal_started, reveal_release=reveal_release,
                           switch_off_started=switch_off_started, switch_off_release=switch_off_release)


async def test_dog_wins_then_searches_nearby_without_metric_location_and_switches_off(game_harness):
    h = game_harness
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    advances = [params for name, params in h.calls if name == "go2_find_object"]
    assert advances == [{"target": "桌上的智能灯"}]
    assert h.searches == [("rps_msg_test_rps_lamp", h.event["deadline"])]
    search_events = [row for row in h.timeline if row.get("action") == "go2_find_object"]
    assert [row["event_type"] for row in search_events] == ["action", "action_result"]
    assert all(row["task_id"] == "rps_msg_test_rps_lamp" for row in search_events)
    off_indices = [index for index, call in enumerate(h.calls)
                   if call == ("SET_STATE", {"state": "OFF"})]
    assert len(off_indices) == 1 and off_indices[0] == len(h.calls)-1
    assert h.calls[-3:] == [("go2_find_object", {"target": "桌上的智能灯"}),
                            ("go2_sport", {"cmd": "Hello"}), ("SET_STATE", {"state": "OFF"})]
    gesture_events = [row for row in h.timeline
                      if row["task_id"] == "rps_msg_test_rps_switch_off_gesture"]
    assert [row["event_type"] for row in gesture_events] == ["action", "action_result"]
    assert all(row["action"] == "go2_sport" and row["arguments"] == {"cmd": "Hello"}
               for row in gesture_events)
    assert gesture_events[-1]["status"] == "ok"
    assert h.timeline.index(gesture_events[-1]) < next(i for i, row in enumerate(h.timeline)
                                                    if row.get("arguments") == {"state": "OFF"})
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
    assert "蓝紫色球体" in question and "不是已看见的证明" in question
    assert "不要仅因色调不完全相同就排除" in question
    cue = next(facts["lamp_identification"] for role, phase, facts in h.decisions
               if role == "go2" and phase == "identify")
    assert cue["color"] == color
    assert h.observations[0]["observation_after"] == cue["confirmed_at"]
    announcement = next(i for i, row in enumerate(h.timeline) if row.get("stage") == "identification")
    assert all(row["event_type"] == "action_result" and row["status"] == "ok"
               for row in h.timeline[announcement - 2:announcement])
    assert any(row.get("stage") == "terms_confirmed" for row in h.timeline[:announcement - 2])
    assert h.timeline[announcement + 1]["event_type"] == "observation"


@pytest.mark.parametrize("receipts", [["accepted"], ["ok", "timeout"]])
async def test_unconfirmed_identification_cannot_announce_or_observe(game_harness, receipts):
    h = game_harness
    h.config.physical_results = receipts
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(name == "go2_observe" for name, _params in h.calls)
    assert not any(row.get("stage") == "identification" for row in h.timeline)
    assert [phase for _role, phase, _facts in h.decisions] == ["setup", "setup", "agree", "agree", "identify"]
    assert h.drive._advance_task.await_args.kwargs["result"]["game_winner"] is None


async def test_lamp_wins_then_model_selected_performance_without_navigation(game_harness):
    h = game_harness
    h.config.lamp_choices = ["paper"]
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(name == "go2_find_object" for name, _params in h.calls)
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
    assert len([1 for name, _ in h.calls if name == "go2_sport"]) == 37  # 36 throws, then symbolic press.
    assert [params for name, params in h.calls if name == "go2_find_object"] == [{"target": "桌上的智能灯"}]
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
    assert any(entry["event_type"] == "action_result" and entry.get("status") == "blocked"
               and entry["task_id"] == "task_test_rps" and "无法开局" in entry["text"]
               for entry in h.timeline)


async def test_unconfirmed_gesture_cannot_score_advance_or_win(game_harness):
    h = game_harness
    h.config.gesture_result = "unknown"
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(name == "go2_find_object" for name, _params in h.calls)
    assert ("SET_STATE", {"state": "OFF"}) not in h.calls
    assert not any(event.get("stage") == "rps_result" for event in h.timeline)
    h.messenger.send_result.assert_called_once_with(
        "msg_test_rps", "unknown", task_id="task_test_rps",
        reason="go2_gesture_unconfirmed:unknown")
    h.navigator.stop_and_confirm.assert_awaited_once()


@pytest.mark.parametrize("observation", [{"result": "error", "message": "无法观察"},
                                         {"result": "ok", "message": "  "}])
async def test_failed_or_empty_observation_keeps_public_rules_but_stops_before_throw(game_harness, observation):
    h = game_harness
    h.config.observation = observation
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert [phase for _role, phase, _facts in h.decisions] == ["setup", "setup", "agree", "agree", "identify"]
    assert [name for name, _ in h.calls] == ["SET_STATE", "SET_COLOR", "go2_observe"]
    assert h.messenger.send_result.call_args.kwargs["reason"] == "game_observation_unavailable"


async def test_unidentifiable_lamp_keeps_agreed_rules_but_never_throws(game_harness):
    h = game_harness
    h.config.target_visible = False
    h.config.observation = {"result": "ok", "message": "画面中没有可辨认的灯或固定标记。"}
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert [phase for _, phase, _ in h.decisions] == ["setup", "setup", "agree", "agree", "identify", "identify"]
    assert len([row for row in h.timeline if row.get("stage") == "mapping_proposal"]) == 2
    assert [name for name, _ in h.calls] == ["SET_STATE", "SET_COLOR", "go2_observe"]
    h.messenger.send_result.assert_called_once_with("msg_test_rps", "rejected", task_id="task_test_rps",
                                                   reason="game_target_not_visible")


async def test_disagreement_stops_before_throw(game_harness):
    h = game_harness
    h.config.agreement = False
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(phase == "throw" for _role, phase, _facts in h.decisions)
    assert h.messenger.send_result.call_args.kwargs["reason"] == "game_terms_not_agreed"
    assert len([row for row in h.timeline if row.get("stage") == "mapping_proposal"]) == 2
    assert not any(row.get("stage") == "terms_confirmed" for row in h.timeline)
    assert not h.calls  # No identification lighting, camera or gestures before agreement.


async def test_public_mapping_is_complete_before_agreement_even_with_vague_model_speech(game_harness):
    h = game_harness
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    stages = [row.get("stage") for row in h.timeline]
    assert stages.index("mapping_proposal") < stages.index("agreement") < stages.index("terms_confirmed")
    assert stages.index("terms_confirmed") < stages.index("identification") < stages.index("recognition")
    assert stages.index("recognition") < stages.index("reveal") < stages.index("rps_result")
    proposals = [row for row in h.timeline if row.get("stage") == "mapping_proposal"]
    assert proposals[0]["unit_id"] == rps_siege.LAMP
    assert proposals[0]["public_result"]["game_rules"]["mapping"] == [
        {"choice": "rock", "label": "红色"}, {"choice": "scissors", "label": "绿色"},
        {"choice": "paper", "label": "蓝色"}]
    assert proposals[0]["text"] == "我选好这局的表达了。"
    assert proposals[1]["unit_id"] == "go2"
    assert proposals[1]["public_result"]["game_rules"]["mapping"] == [
        {"choice": "rock", "label": "挥手"}, {"choice": "scissors", "label": "伸展"},
        {"choice": "paper", "label": "开心"}]
    agreements = [facts for _, phase, facts in h.decisions if phase == "agree"]
    throws = [facts for _, phase, facts in h.decisions if phase == "throw"]
    assert all(facts["canonical_terms"] == agreements[0]["canonical_terms"] for facts in agreements + throws)
    assert all("choices" not in facts for facts in agreements + throws)
    assert all(facts["target_identification"] is None for _, phase, facts in h.decisions if phase in {"setup", "agree"})
    assert all(facts["target_identification"]["target_visible"] for facts in throws)
    assert all(set(params) == {"r", "g", "b", "brightness"}
               for name, params in h.calls if name == "SET_COLOR")


def test_mapping_requires_all_three_readable_color_names():
    proposal = {"speech": "我选好了。", "mapping": {
        "rock": {"r": 255, "g": 0, "b": 0, "brightness": 160},
        "scissors": {"r": 0, "g": 255, "b": 0, "brightness": 160},
        "paper": {"r": 0, "g": 0, "b": 255, "brightness": 160}},
        "color_names": {"rock": "红色", "scissors": "绿色", "paper": "蓝色"}}
    assert LampSetup.model_validate(proposal).color_names.paper == "蓝色"
    for names in ({"rock": "红色"}, {**proposal["color_names"], "paper": "  "}):
        with pytest.raises(ValueError):
            LampSetup.model_validate({**proposal, "color_names": names})


@pytest.mark.parametrize("slow_role", ["lamp", "go2"])
async def test_both_throws_dispatch_before_either_receipt_and_score_waits_for_both(game_harness, slow_role):
    h = game_harness
    h.config.concurrent_reveal = True
    h.reveal_release[slow_role].clear()
    task = asyncio.create_task(rps_siege.GAME.execute(h.drive, h.event, "task_test_rps"))
    try:
        for started in h.reveal_started.values():
            await asyncio.wait_for(started.wait(), timeout=2)
        assert not task.done()
        assert not any(row.get("stage") == "rps_result" for row in h.timeline)
        assert not any(name == "go2_find_object" for name, _ in h.calls)
        assert len([phase for _, phase, _ in h.decisions if phase == "throw"]) == 2
    finally:
        h.reveal_release[slow_role].set()
        await asyncio.wait_for(task, timeout=2)
    assert any(row.get("stage") == "rps_result" for row in h.timeline)


async def test_failed_concurrent_lamp_throw_never_scores_or_rewards(game_harness):
    h = game_harness
    h.config.concurrent_reveal = True
    h.config.physical_results = ["ok", "ok", "unknown"]
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert all(started.is_set() for started in h.reveal_started.values())
    assert not any(row.get("stage") == "rps_result" for row in h.timeline)
    assert not any(name == "go2_find_object" for name, _ in h.calls)
    assert h.messenger.send_result.call_args.kwargs["reason"] == "lamp_action_unconfirmed:unknown"
    h.navigator.stop_and_confirm.assert_awaited_once()


async def test_user_interruption_cancels_both_pending_reveal_waiters(game_harness):
    h = game_harness
    h.config.concurrent_reveal = True
    for release in h.reveal_release.values():
        release.clear()
    task = asyncio.create_task(rps_siege.GAME.execute(h.drive, h.event, "task_test_rps"))
    try:
        for started in h.reveal_started.values():
            await asyncio.wait_for(started.wait(), timeout=2)
        h.drive.user_interrupt = True
        await asyncio.wait_for(task, timeout=2)
    finally:
        for release in h.reveal_release.values():
            release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not any(row.get("stage") == "rps_result" for row in h.timeline)
    assert h.messenger.send_result.call_args.kwargs["reason"] == "autonomy_interrupted_or_offline"
    h.navigator.stop_and_confirm.assert_awaited_once()


@pytest.mark.parametrize("phase", ["setup", "agree"])
async def test_intervention_during_decision_cannot_publish_stale_rules_or_throw(game_harness, phase):
    h = game_harness
    h.config.interrupt_decision = ("lamp", phase)
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert not any(p == "throw" for _, p, _ in h.decisions)
    forbidden = "mapping_proposal" if phase == "setup" else "terms_confirmed"
    assert not any(row.get("stage") == forbidden for row in h.timeline)
    assert h.messenger.send_result.call_args.kwargs["reason"] == "autonomy_interrupted_or_offline"


@pytest.mark.parametrize("patch", [
    {"result": "accepted"},
    {"outcome": "seen_not_reached"},
    {"outcome": None},
    {"found": False},
    {"stop": {"result": "accepted"}},
    {"stop": None},
    {"target": "另一个小球"},
    {"task_id": "other_search"},
])
async def test_unconfirmed_search_keeps_dog_win_but_never_switches_off(game_harness, patch):
    h = game_harness
    h.config.search_results[0].update(patch)
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert ("SET_STATE", {"state": "OFF"}) not in h.calls
    assert not h.switch_off_started.is_set()
    assert h.messenger.send_result.call_args.kwargs["reason"].startswith("go2_won:game_approach_unconfirmed:")
    assert any(row.get("stage") == "rps_result" and row.get("reason") == "go2_won" for row in h.timeline)
    assert h.timeline[-1]["public_result"]["reason"].startswith("go2_won:")
    assert h.timeline[-1]["status"] != "ok"


async def test_navigation_failure_does_not_change_winner(game_harness):
    h = game_harness
    h.navigator.snapshot = lambda: (None, "map_unavailable")
    h.config.search_results = [{"result": "blocked", "reason": "map_unavailable"}]
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


@pytest.mark.parametrize("result", ["accepted", "executing", "unknown", "error", "timeout"])
async def test_unconfirmed_symbolic_press_preserves_dog_win_without_switching_off(game_harness, result):
    h = game_harness
    h.config.switch_off_gesture_result = result
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert h.switch_off_started.is_set()
    assert ("SET_STATE", {"state": "OFF"}) not in h.calls
    assert h.messenger.send_result.call_args.kwargs["reason"] == "go2_won:game_switch_off_gesture_unconfirmed:" + result
    assert h.drive._advance_task.await_args.kwargs["result"]["game_winner"] == "go2"
    assert h.timeline[-1]["public_result"]["reason"].startswith("go2_won:")
    assert h.timeline[-1]["status"] != "ok"
    h.navigator.stop_and_confirm.assert_awaited_once()
    h.agent.end_peer_game.assert_called_once_with("rps:msg_test_rps", preserve_output=False)


@pytest.mark.parametrize("interrupt", [False, True])
async def test_symbolic_press_waits_for_completion_and_respects_user_interruption(game_harness, interrupt):
    h = game_harness
    h.switch_off_release.clear()
    task = asyncio.create_task(rps_siege.GAME.execute(h.drive, h.event, "task_test_rps"))
    try:
        await asyncio.wait_for(h.switch_off_started.wait(), timeout=2)
        assert not task.done()
        assert ("SET_STATE", {"state": "OFF"}) not in h.calls
        assert not any(row.get("stage") == "settlement" for row in h.timeline)
        h.drive.user_interrupt = interrupt
        h.switch_off_release.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        h.switch_off_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert (("SET_STATE", {"state": "OFF"}) in h.calls) is not interrupt
    if interrupt:
        assert h.messenger.send_result.call_args.kwargs["reason"] == "go2_won:autonomy_interrupted_or_offline"
        h.agent.end_peer_game.assert_called_once_with("rps:msg_test_rps", preserve_output=False)
    else:
        assert h.messenger.send_result.call_args.kwargs["reason"] == "go2_won_lamp_off_confirmed"


async def test_unconfirmed_performance_preserves_lamp_win(game_harness):
    h = game_harness
    h.config.lamp_choices = ["paper"]
    h.config.performance_result = "accepted"
    await rps_siege.GAME.execute(h.drive, h.event, "task_test_rps")
    assert h.messenger.send_result.call_args.kwargs["reason"] == "lamp_won:game_performance_unconfirmed:accepted"
    assert h.drive._advance_task.await_args.kwargs["result"]["game_winner"] == "lamp"
    assert h.timeline[-1]["public_result"]["reason"].startswith("lamp_won:")
    assert h.timeline[-1]["status"] != "ok"


async def test_user_interruption_after_arrival_prevents_switch_off(game_harness):
    h = game_harness
    h.config.interrupt_after = "go2_find_object"
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
    setup = {"speech": "看不到灯，先不开局。", "target_visible": False, "target": None}
    assert Go2Identification.model_validate(setup).target is None
    with pytest.raises(ValueError, match="unobserved_game_target"):
        Go2Identification.model_validate({**setup, "target": "猜测的灯"})
    with pytest.raises(ValueError, match="visible_game_target_required"):
        Go2Identification.model_validate({**setup, "target_visible": True})
    assert Go2Identification.model_validate({**setup, "target_visible": True, "target": " 桌上的灯 "}).target == "桌上的灯"


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


async def test_public_round_is_receipt_gated_and_throw_speech_precedes_verdict(game_harness):
    h = game_harness
    await rps_siege.GAME.execute(h.drive, h.event, 'task_test_rps')
    verdict_index, verdict = next((i, row) for i, row in enumerate(h.timeline)
                                  if row.get('stage') == 'rps_result')
    public = verdict['game_round']
    assert public['round'] == 1 and public['winner'] == 'go2'
    assert public['choices'] == {rps_siege.LAMP: 'scissors', 'go2': 'rock'}
    assert public['mapping']['go2']['arguments'] == {'cmd': 'Hello'}
    assert public['mapping'][rps_siege.LAMP]['choice'] == 'scissors'
    announcements = [i for i, row in enumerate(h.timeline) if row.get('stage') == 'throw_announcement']
    reveal = next(i for i, row in enumerate(h.timeline) if row.get('stage') == 'reveal')
    assert len(announcements) == 2 and reveal < min(announcements) < max(announcements) < verdict_index
    receipts = [row for row in h.timeline[max(announcements)+1:verdict_index] if row['event_type'] == 'action_result']
    assert len(receipts) == 2 and all(row['status'] == 'ok' for row in receipts)
    assert all('game_round' not in row for row in h.timeline[:verdict_index])


async def test_unknown_throw_never_publishes_structured_choices(game_harness):
    h = game_harness
    h.config.gesture_result = 'unknown'
    await rps_siege.GAME.execute(h.drive, h.event, 'task_test_rps')
    assert all('game_round' not in row for row in h.timeline)


async def test_silent_game_decisions_keep_public_mapping_and_real_round_result(game_harness, monkeypatch):
    h = game_harness
    decide = rps_siege.decide

    async def silent_internal_choices(role, phase, facts, **kwargs):
        answer = await decide(role, phase, facts, **kwargs)
        if phase in {"setup", "agree", "throw", "performance"}:
            answer.pop("speech", None)
        return answer

    monkeypatch.setattr(rps_siege, "decide", silent_internal_choices)
    await rps_siege.GAME.execute(h.drive, h.event, 'task_test_rps')
    proposals = [row for row in h.timeline if row.get('stage') == 'mapping_proposal']
    assert len(proposals) == 2
    assert all(row['event_type'] == 'action_result' and not row['text'] for row in proposals)
    assert all(len(row['public_result']['game_rules']['mapping']) == 3 for row in proposals)
    assert not any(row['event_type'] == 'message' and row.get('stage') in {
        'mapping_proposal', 'agreement', 'throw_announcement'} for row in h.timeline)
    verdict = next(row for row in h.timeline if row.get('stage') == 'rps_result')
    assert verdict['game_round']['winner'] == 'go2'
    assert any(row['event_type'] == 'action_result' and row.get('action') == 'go2_sport'
               and row.get('status') == 'ok' for row in h.timeline)
