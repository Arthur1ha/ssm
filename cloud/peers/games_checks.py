"""Offline peer game protocol checks; run with ``uv run python -m cloud.peers.games_checks``."""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cloud.mqtt_signing import sign_message
from cloud.peers.games import Game, GAMES, playable_games
from cloud.peers.messaging import AgentMessenger
from cloud.space.execution import ExecutionContext


class _Activity:
    def __init__(self) -> None:
        self.value = {"activity_id": "act_test", "status": "active",
                      "started_at": time.time() - 20, "devices": {
            "esp32_desk_led": {"status": "joined"}, "go2": {"status": "joined"}}}

    def current(self):
        return self.value


    def record_event(self, *_args, **_kwargs):
        return {"recorded": True}


class _Mqtt:
    def __init__(self) -> None:
        self.sent = []

    def publish(self, topic, payload, **_kwargs):
        self.sent.append((topic, payload))
        return type("Receipt", (), {"rc": 0})()


def check() -> None:
    os.environ.setdefault("SSM_MQTT_SIGNING_SECRET", "ssm-game-checks-secret")
    store, mqtt, delivered = _Activity(), _Mqtt(), []
    game = Game("color_hunt", "寻找同色物品", "灯亮颜色，狗找同色物品；找到就算狗赢",
                ("esp32_desk_led", "go2"), lambda _sender, _target: True,
                lambda *_args: None, "我换灯色，你找同色物品。")
    with (patch("cloud.space.activity.get_activity_store", return_value=store),
          patch.dict("cloud.peers.games.GAMES", {"color_hunt": game}, clear=True)):
        messenger = AgentMessenger(mqtt, None, None,
                                   {"esp32_desk_led", "go2"}, delivered.append)
        messenger._allowed = lambda _sender, _target: True
        messenger._decision_context = lambda _sender: _observed_space()
        from cloud.space.decisions import decision_evidence
        invitation_ref = next(item["ref"] for item in decision_evidence(_observed_space())
                              if item["kind"] == "observation")
        invitation_basis = {"decision_reason": "狗刚分享了环境观察，想邀请它一起找颜色。",
                            "evidence_refs": [invitation_ref]}
        try:
            assert [item["game_id"] for item in playable_games(
                "act_test", "esp32_desk_led", "go2")] == ["color_hunt"]
            assert playable_games("act_test", "go2", "esp32_desk_led") == []
            sent = messenger.send_message("esp32_desk_led", "go2",
                "来玩找颜色？我亮一种颜色，你找同色物品，找到了就算你赢。",
                interaction_kind="game_invite", game_id="color_hunt", **invitation_basis)
            assert sent["result"] == "accepted"
            assert 290 < messenger.get_status(sent["message_id"])["deadline"] - time.time() <= 300
            topic, raw = mqtt.sent[-1]
            assert json.loads(raw)["content"] == "来玩找颜色？我亮一种颜色，你找同色物品，找到了就算你赢。"
            assert messenger.handle_message(topic, raw)
            messenger._workers.submit(lambda: None).result()
            assert len(delivered) == 1 and delivered[0]["game_id"] == "color_hunt"
            accepted = messenger.send_message("go2", "esp32_desk_led", "同意，等你出题",
                reply_to=sent["message_id"], reply_kind="accepted")
            assert accepted["result"] == "accepted"
            reply_topic, reply_raw = mqtt.sent[-1]
            assert messenger.handle_message(reply_topic, reply_raw)
            assert messenger.get_status(sent["message_id"])["state"] == "waiting_start"
            assert messenger.send_progress(accepted["message_id"], "task_" + "a" * 32)["result"] == "accepted"
            assert messenger.send_result(accepted["message_id"], "ok",
                task_id="task_" + "a" * 32)["result"] == "accepted"
            for result_topic, result_raw in mqtt.sent[-2:]:
                assert messenger.handle_message(result_topic, result_raw)
            messenger._workers.submit(lambda: None).result()
            assert messenger.get_status(accepted["message_id"])["state"] == "ok"
            assert messenger.handle_message(topic, raw)
            assert len([item for item in delivered if item.get("game_id") == "color_hunt"]) == 1
            payload = json.loads(raw)
            payload.pop("_mqtt_auth")
            payload["game_id"] = "unknown"
            assert messenger.handle_message(topic, json.dumps(sign_message(topic, payload)))
            assert len([item for item in delivered if item.get("game_id") == "color_hunt"]) == 1
            payload["game_id"] = "color_hunt"
            payload["deadline"] = time.time() - 1
            assert messenger.handle_message(topic, json.dumps(sign_message(topic, payload)))
            assert len([item for item in delivered if item.get("game_id") == "color_hunt"]) == 1
            payload["deadline"] = time.time() + 30
            payload["message_id"] = payload["conversation_id"] = "msg_cross_activity"
            store.value["activity_id"] = "act_other"
            assert messenger.handle_message(topic, json.dumps(sign_message(topic, payload)))
            assert len([item for item in delivered if item.get("game_id") == "color_hunt"]) == 1
            store.value["activity_id"] = "act_test"
            assert messenger.send_message("esp32_desk_led", "go2", "来玩",
                interaction_kind="game_invite", game_id="color_hunt", **invitation_basis)["result"] == "accepted"
            assert json.loads(mqtt.sent[-1][1])["content"] == game.invitation
            store.value["devices"]["go2"]["status"] = "left"
            assert playable_games("act_test", "esp32_desk_led", "go2") == []
            assert messenger.send_message("esp32_desk_led", "go2", "来玩",
                interaction_kind="game_invite", game_id="color_hunt", **invitation_basis)["result"] == "blocked"
            store.value = None
            assert messenger.send_message("esp32_desk_led", "go2", "普通消息")["result"] == "accepted"
        finally:
            messenger.close()


def check_invitation_needs_no_existing_color() -> None:
    store = _Activity()
    with (patch("cloud.space.activity.get_activity_store", return_value=store),
          patch("cloud.peers.games.live_lamp_color", return_value=None)):
        assert [item["game_id"] for item in playable_games(
            "act_test", "esp32_desk_led", "go2")] == ["color_hunt"]


def check_game_commands_do_not_start_user_hold() -> None:
    """The lamp's own game tasks must leave its reactive setup running."""
    from cloud.api.main import _on_esp32_message

    held = []
    agent = SimpleNamespace(mark_user_command=lambda unit_id: held.append(unit_id))
    sink = SimpleNamespace(handle_message=lambda *_args, **_kwargs: None)
    with (patch("cloud.api.main.get_registry", return_value=sink),
          patch("cloud.api.main.get_constraint_store", return_value=sink),
          patch("cloud.api.main.get_activity_store", return_value=sink),
          patch("cloud.api.main._agent_messenger", None),
          patch("cloud.api.main.peer_gateway.handle_peer_result", return_value=False),
          patch("cloud.api.main.intents_module.handle_mqtt_message"),
          patch("cloud.api.main.esp32_agent_mod.get_agent", return_value=agent)):
        for source in ("peer_game_color", "peer_game_twist", "peer_autonomous"):
            message = SimpleNamespace(topic="ssm/task/esp32_desk_led/task_1",
                payload=json.dumps({"session_id": "peer_invite", "source": source}).encode(),
                retain=False)
            _on_esp32_message(None, None, message)
        assert not held
        message.payload = json.dumps({"session_id": "user_session", "source": "orchestrator"}).encode()
        _on_esp32_message(None, None, message)
    assert held == ["esp32_desk_led"]


def check_delegated_game_handoff() -> None:
    """Existing activity and send_message tools hand the request to lamp."""
    from cloud.agents.steward.tools import make_executor
    from cloud.agents.lamp.tools.execute import execute_lamp_tool

    store = _Activity()
    sent = []
    messenger = SimpleNamespace(send_message=lambda *args, **kwargs:
        sent.append((args, kwargs)) or {"result": "accepted", "message_id": "msg_1"})
    run = SimpleNamespace(run_id="run_game", session_id="user_session",
                          goal="让智能灯邀请机器狗一起玩一局", deadline=time.time() + 60,
                          steps=[])
    context = {"session_id": "user_session", "actor": "visitor", "persist": False,
               "space_id": "main", "origin_kind": "user_delegated",
               "expires_at": time.time() + 60, "event_id": "evt_game",
               "user_revision": 1}

    async def handoff():
        with (patch("cloud.agents.steward.tools.transport.do_start_activity",
                    return_value={"activity": store.value}),
              patch("cloud.agents.steward.tools.transport._messenger", messenger, create=True)):
            execute = make_executor(run, context)
            started = await execute("start_activity", {}, "step_start")
            assert started["result"] == "ok"
            run.steps.append({"tool": "start_activity", "result": started})
            delivered = await execute("send_message", {"target": "esp32_desk_led",
                "content": run.goal}, "step_handoff")
            assert delivered["result"] == "accepted"
            assert sent[-1][0][:3] == ("cloud_orchestrator", "esp32_desk_led", run.goal)
    asyncio.run(handoff())

    social = SimpleNamespace(activity_id="act_test", can_send=lambda *_a: True,
                             sent=lambda *args: sent.append(("social_sent", args)))
    agent = SimpleNamespace(_state=object(), _social_state=social)
    with (patch("cloud.space.activity.get_activity_store", return_value=store),
          patch("cloud.peers.social.get_activity_store", return_value=store),
          patch("cloud.space.context.get_space_context", return_value={**_observed_space(), "peers": [
              {"unit_id": "go2", "online": True}]}),
          patch("cloud.agents.lamp.tools.execute.execute_auxiliary",
                return_value={"result": "accepted", "message_id": "invite_1"})):
        result = execute_lamp_tool("send_message", {"target": "go2",
            "content": "来玩找颜色？我亮一种颜色，你找同色物品，找到了就算你赢。", "interaction_kind": "game_invite",
            "game_id": "color_hunt", "decision_reason": "狗刚观察到物品，邀请它一起玩。",
            "evidence_refs": ["evt_observed"]}, "call_1", agent=agent,
            unit_id="esp32_desk_led",
            context=ExecutionContext("peer_msg", "visitor", False, "main",
                                     "agent_autonomous", int(time.time() + 60)),
            dispatch=None, lighting_only=False, run_id="lamp_game")
    assert result["result"] == "accepted", result
    assert sent[-1][0] == "social_sent"


def _observed_space(summary: str = "地上有一个红色球") -> dict:
    return {"peers": [], "environment": [], "constraints": [],
            "activity": {"activity_id": "act_test", "status": "active",
                         "started_at": time.time() - 20, "context": "自由活动"},
            "recent_discoveries": [{"event_id": "evt_observed", "activity_id": "act_test",
                "unit_id": "go2", "source_unit_id": "go2", "event_type": "observation",
                "ts": time.time(), "summary": summary}]}


def check_lamp_sets_color_after_acceptance() -> None:
    """Different observed scenes support different graph-selected colors, no preset target."""
    from cloud.peers.inbound import _handle_color_hunt_acceptance
    from cloud.space.decisions import decision_evidence, resolve_decision

    for summary, rgb, reason in (("地上有一个红色球", (255, 0, 0), "狗刚看到红色球，红色有物品可找。"),
                                 ("架子旁有蓝色盒子", (0, 0, 255), "狗刚看到蓝色盒子，选蓝色有观察依据。")):
        receipts, events, dispatches, setup_calls = [], [], [], []
        space = _observed_space(summary)
        reference = next(item["ref"] for item in decision_evidence(space) if item["kind"] == "observation")
        basis = resolve_decision(space, reason, [reference], require_observation=True)
        message = {"message_id": "accept_1", "conversation_id": "invite_1",
                   "reply_to": "invite_1", "sender": "go2", "target": "esp32_desk_led",
                   "activity_id": "act_test", "deadline": time.time() + 60}
        messenger = SimpleNamespace(
            get_status=lambda _id: {"interaction_kind": "game_invite", "state": "waiting_start",
                "sender": "esp32_desk_led", "game_id": "color_hunt", "target": "go2", "activity_id": "act_test"},
            send_progress=lambda *args: receipts.append(("progress", args)),
            send_result=lambda *args, **kwargs: receipts.append(("result", args, kwargs)))

        def run_lamp(*args, **kwargs):
            setup_calls.append((args, kwargs))
            assert "优先" not in args[0] and "白色" not in args[0] and "猫头鹰" not in args[0]
            assert "decision_reason" in args[0] and "evidence_refs" in args[0]
            assert args[2].session_id.startswith("peer_game_color_")
            params = dict(zip(("r", "g", "b"), rgb), brightness=180)
            task_id = args[3]("esp32_desk_led", "SET_COLOR", params,
                              args[2], "lamp_semantic", task_id="task_" + "a" * 32)
            return {"result": "ok", "steps": [{"tool": "set_led_color", "params": params,
                    "result": {"result": "ok", "task_id": task_id, "decision_basis": basis}}]}

        agent = SimpleNamespace(_state=object(), _should_act=lambda: True, _device_online=lambda: True,
                                run_user_loop=run_lamp)
        activity = SimpleNamespace(record_event=lambda *args, **kwargs: events.append((args, kwargs)))
        color = {"rgb": rgb, "brightness": 180, "as_of": time.time() + 1}
        with (patch("cloud.peers.games.activity_allows", return_value=True),
              patch("cloud.peers.games.live_lamp_color", return_value=color),
              patch("cloud.space.context.get_space_context", return_value=space),
              patch("cloud.esp32.service.get_agent", return_value=agent),
              patch("cloud.api.intents._dispatch_mqtt_task", side_effect=lambda *a, **k:
                    dispatches.append((a, k)) or "lamp_color_1"),
              patch("cloud.space.activity.get_activity_store", return_value=activity)):
            _handle_color_hunt_acceptance(messenger, message, object())
        assert len(dispatches) == 1 and setup_calls[0][1]["lighting_only"] is True
        assert receipts[-1] == ("result", ("accept_1", "ok"), {"task_id": "lamp_color_1"})
        assert events[-1][1]["decision_basis"] == basis
        assert events[-1][1]["trigger_message_id"] == "invite_1"
        assert not {"decision_reason", "evidence_refs"} & dispatches[0][0][2].keys()


def check_no_observation_no_game_color() -> None:
    """No current shared visual evidence means no graph call or physical power-on."""
    from cloud.peers.inbound import _handle_color_hunt_acceptance
    receipts = []
    message = {"message_id": "accept_empty", "conversation_id": "invite_empty",
               "reply_to": "invite_empty", "activity_id": "act_test", "deadline": time.time() + 60}
    messenger = SimpleNamespace(get_status=lambda _id: {
        "interaction_kind": "game_invite", "state": "waiting_start", "sender": "esp32_desk_led",
        "game_id": "color_hunt", "target": "go2", "activity_id": "act_test"},
        send_result=lambda *a, **kw: receipts.append((a, kw)))
    agent = SimpleNamespace(_state=object(), _should_act=lambda: True, _device_online=lambda: True,
                            run_user_loop=AsyncMock())
    space = _observed_space()
    space["recent_discoveries"] = []
    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.space.context.get_space_context", return_value=space),
          patch("cloud.esp32.service.get_agent", return_value=agent),
          patch("cloud.api.intents._dispatch_mqtt_task") as dispatch,
          patch("cloud.space.activity.get_activity_store", return_value=_Activity())):
        _handle_color_hunt_acceptance(messenger, message, object())
    dispatch.assert_not_called()
    agent.run_user_loop.assert_not_called()
    assert receipts[-1] == (("accept_empty", "blocked"), {"reason": "game_observation_required"})


async def check_graph_handoff(confirmation: str = "odom_stopped_and_visual_recheck") -> None:
    """The existing graph chooses its target; success never scripts additional motion or lamp changes."""
    receipts, activity_events, replies = [], [], []
    lamp_rgb = [(255, 255, 255)]
    def activity_event(kind, text, **facts):
        activity_events.append((kind, text, facts))
        return {"event_id": "evt_" + str(len(activity_events)), "ts": time.time()}

    drive = SimpleNamespace(
        _active_goal={"kind": "peer_message", "event_id": "game_check"},
        _context=ExecutionContext("act_test", "visitor", False, "main", "agent_autonomous"),
        _activity_id=lambda: "act_test",
        _activity_event=activity_event,
        _advance_task=AsyncMock(return_value={"next_action": "run_game"}), user_interrupt=False)
    messenger = SimpleNamespace(
        send_result=lambda *args, **kwargs: receipts.append((args, kwargs)),
        send_message=lambda *args, **kwargs: replies.append((args, kwargs)) or {"result": "accepted"},
        get_status=lambda _message_id: {"state": "ok"})
    event = {"event_id": "game_check", "message_id": "game_check",
             "source": "esp32_desk_led", "activity_id": "act_test",
             "acceptance_message_id": "accept_game_check", "acceptance_sent_at": time.time(),
             "deadline": time.time() + 30,
             "message": {"sender": "esp32_desk_led", "content": "来找同色物品"}, "messenger": messenger}

    async def graph(_session_id, goal, **kwargs):
        assert kwargs["publish_reply"] is False
        assert "毛绒" not in goal and "猫头鹰" not in goal
        assert kwargs["allowed_tools"] == frozenset({
            "go2_observe", "go2_navigate_to_visible_target", "get_space_context"})
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "白色盒子"})["result"] == "blocked"
        assert kwargs["tool_guard"]("go2_observe", {"question": "找白色物品"}) is None
        kwargs["on_step"]({"tool": "go2_observe", "result": {"result": "error", "message": "无可用视频帧"}})
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "白色盒子"})["result"] == "blocked"
        assert not any(kind == "observation" for kind, _text, _facts in activity_events)
        kwargs["on_step"]({"tool": "go2_observe", "result": {"result": "ok", "message": "看到白色盒子"}})
        lamp_rgb[0] = (0, 0, 255)
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "白色盒子"})["result"] == "blocked"
        lamp_rgb[0] = (255, 255, 255)
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "盒子"})["result"] == "blocked"
        missing_reason = kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "白色盒子"})
        assert missing_reason["error"] == "decision_reason_required"
        assert not any(kind == "message" for kind, _text, _facts in activity_events)
        reason = "我刚看见地上的白色盒子，颜色与灯一致，我想先检查能否靠近它。"
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {
            "target": "白色盒子", "decision_reason": reason}) is None
        assert activity_events[-1][1] == reason
        assert json.loads(kwargs["task_context"]())["latest_observation"]["summary"] == "看到白色盒子"
        return {"result": "ok", "run_id": "game_check", "tool_results": [
            {"tool": "go2_navigate_to_visible_target", "params": {"target": "白色盒子"}, "result": {
                "result": "ok", "confirmation": confirmation}}]}

    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.peers.games.live_lamp_color", side_effect=lambda _source: {
              "rgb": lamp_rgb[0], "source": "esp32_desk_led", "as_of": time.time()}),
          patch("cloud.go2.connection.go2", SimpleNamespace(is_connected=True, send_command=AsyncMock(return_value={"result": "ok"}))),
          patch("cloud.go2.navigation.navigator.navigator._snapshot", return_value=(({}, object()), None)),
          patch("cloud.go2.navigation.navigator.navigator.go_to_coordinate") as home,
          patch("cloud.agents.go2.graph.run_go2_loop", graph),
          patch("cloud.go2.tools.executor.execute_tool") as sport,
          patch("cloud.api.intents._dispatch_mqtt_task") as lamp,
          patch("cloud.peers.gateway.request_peer_skill") as off):
        await GAMES["color_hunt"].execute(drive, event, "peer_game_check")
    home.assert_not_called()
    sport.assert_not_called()
    lamp.assert_not_called()
    off.assert_not_called()
    expected = "ok" if confirmation == "odom_stopped_and_visual_recheck" else "unknown"
    assert receipts[0][0][1] == expected
    assert receipts[0][1]["reason"] == ("candidate_reached" if expected == "ok" else "no_confirmed_color_match")
    assert "plot_steps" not in drive._advance_task.await_args.kwargs["result"]
    assert ("这局找到了" in replies[-1][0][2]) is (expected == "ok")
    candidate = next(facts for kind, _text, facts in activity_events
                     if kind == "message" and facts.get("decision_basis"))
    observation = next((kind, text, facts) for kind, text, facts in activity_events if kind == "observation")
    assert observation[1] == "看到白色盒子"
    assert candidate["decision_basis"]["evidence"][0]["summary"] == observation[1]
    assert "evt_" in candidate["decision_basis"]["evidence"][0]["ref"]


async def check_navigation_reason_is_narration_only() -> None:
    """Optional model narration is schema-valid and never becomes a motion argument."""
    from cloud.go2.tools.registry import get_tool_spec
    from cloud.go2.tools.executor import validate_params
    from cloud.go2.tools.tools import go2_navigate_to_visible_target

    reason = "我刚看到地上的红球，它与灯的题目颜色一致，我想过去确认。"
    spec = get_tool_spec("go2_navigate_to_visible_target")
    assert validate_params(spec.params_schema, {"target": "红球"}) == {"target": "红球"}
    args = validate_params(spec.params_schema, {"target": "红球", "decision_reason": reason})
    with patch("cloud.go2.navigation.target_approach.navigate_to_visible_target", new_callable=AsyncMock,
               return_value={"result": "blocked", "reason": "test_no_motion"}) as navigate:
        await go2_navigate_to_visible_target(**args)
        await go2_navigate_to_visible_target("红球")
    assert navigate.await_count == 2
    assert all(call.args == ("红球",) and not call.kwargs for call in navigate.await_args_list)


async def check_game_waits_for_lamp_setup() -> None:
    from cloud.peers.games import _run_color_hunt

    receipts = []
    drive = SimpleNamespace(_active_goal={"event_id": "game_wait"},
        _activity_id=lambda: "act_test", user_interrupt=False,
        _advance_task=AsyncMock())
    messenger = SimpleNamespace(
        get_status=lambda _id: {"state": "rejected",
            "result": {"reason": "lamp_color_unknown_or_stale"}},
        send_result=lambda *args, **kwargs: receipts.append((args, kwargs)),
        send_message=lambda *_args, **_kwargs: None)
    event = {"message_id": "game_wait", "acceptance_message_id": "accept_wait",
        "activity_id": "act_test", "source": "esp32_desk_led",
        "deadline": time.time() + 30,
        "message": {"sender": "esp32_desk_led"}, "messenger": messenger}
    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.agents.go2.graph.run_go2_loop", new_callable=AsyncMock) as graph):
        await _run_color_hunt(drive, event, "peer_game_wait")
    graph.assert_not_awaited()
    assert receipts[0][0][1] == "rejected"
    assert receipts[0][1]["reason"] == "lamp_color_unknown_or_stale"


if __name__ == "__main__":
    check()
    check_invitation_needs_no_existing_color()
    check_game_commands_do_not_start_user_hold()
    check_delegated_game_handoff()
    check_lamp_sets_color_after_acceptance()
    check_no_observation_no_game_color()
    asyncio.run(check_graph_handoff())
    asyncio.run(check_graph_handoff("odom_stopped_near_mapped_stop"))
    asyncio.run(check_game_waits_for_lamp_setup())
    asyncio.run(check_navigation_reason_is_narration_only())
    print("peer game protocol checks passed")
