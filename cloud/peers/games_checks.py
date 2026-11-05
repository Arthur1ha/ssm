"""Offline peer game protocol checks; run with ``uv run python -m cloud.peers.games_checks``."""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

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
        try:
            assert [item["game_id"] for item in playable_games(
                "act_test", "esp32_desk_led", "go2")] == ["color_hunt"]
            assert playable_games("act_test", "go2", "esp32_desk_led") == []
            sent = messenger.send_message("esp32_desk_led", "go2",
                "来玩找颜色？我亮一种颜色，你找同色物品，找到了就算你赢。",
                interaction_kind="game_invite", game_id="color_hunt")
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
                interaction_kind="game_invite", game_id="color_hunt")["result"] == "accepted"
            assert json.loads(mqtt.sent[-1][1])["content"] == game.invitation
            store.value["devices"]["go2"]["status"] = "left"
            assert playable_games("act_test", "esp32_desk_led", "go2") == []
            assert messenger.send_message("esp32_desk_led", "go2", "来玩",
                interaction_kind="game_invite", game_id="color_hunt")["result"] == "blocked"
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


async def check_game_line_uses_lamp_role() -> None:
    from cloud.peers.games import _game_line

    model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content="来玩找颜色？")))
    with patch("cloud.agents.common.models.get_text_model", return_value=model) as create:
        assert await _game_line("esp32_desk_led", "邀请玩游戏", "兜底") == "来玩找颜色？"
    create.assert_called_once_with("lamp")


async def check_timed_activity_invite() -> None:
    """After the join delay, one invite is sent through the existing game bus."""
    from cloud.peers.games import invite_color_hunt_for_activity
    from cloud.peers.social import SocialState

    store, sent, visible = _Activity(), [], []
    store.events = lambda _activity_id: list(visible)
    social = SocialState()
    social.reset("act_test")
    social.sent("go2", "普通消息")
    agent = SimpleNamespace(_should_act=lambda: True, _device_online=lambda: True,
                            _social_state=social)

    def send_message(sender, target, content, **kwargs):
        sent.append((sender, target, content, kwargs))
        visible.append({"event_type": "message", "unit_id": sender,
                        "interaction_kind": kwargs["interaction_kind"],
                        "game_id": kwargs["game_id"]})
        return {"result": "accepted", "message_id": "invite_test"}

    messenger = SimpleNamespace(send_message=send_message)
    with (patch("cloud.space.activity.get_activity_store", return_value=store),
          patch("cloud.esp32.service.get_agent", return_value=agent),
          patch("cloud.esp32.router.get_activity_mode_owner", return_value="act_test"),
          patch("cloud.go2.router.get_activity_mode_owner", return_value="act_test"),
          patch("cloud.go2.mode.free_explore.drive._activity_id", return_value="act_test"),
          patch("cloud.go2.connection.go2.is_connected", True),
          patch("cloud.api.main.get_agent_messenger", return_value=messenger),
          patch("cloud.personality.names.agent_names", return_value={"go2": "呆呆狗"}),
          patch("cloud.peers.games._game_line", new_callable=AsyncMock,
                return_value="呆呆狗，来玩找颜色？我亮一种颜色，你找同色的东西，找到了算你赢。") as line):
        store.value["started_at"] = time.time()
        assert playable_games("act_test", "esp32_desk_led", "go2") == []
        assert (await invite_color_hunt_for_activity("act_test"))["reason"] == "invite_delay"
        assert not sent
        store.value["started_at"] -= 20
        assert (await invite_color_hunt_for_activity("act_test"))["result"] == "accepted"
        assert (await invite_color_hunt_for_activity("act_test"))["reason"] == "already_invited"
        assert len(sent) == 1
        assert sent[0][0:2] == ("esp32_desk_led", "go2")
        assert sent[0][2] == "呆呆狗，来玩找颜色？我亮一种颜色，你找同色的东西，找到了算你赢。"
        assert sent[0][3] == {"interaction_kind": "game_invite", "game_id": "color_hunt"}
        line.assert_awaited_once()
        store.value["status"] = "ended"
        assert (await invite_color_hunt_for_activity("act_test"))["reason"] == "activity_unavailable"


async def check_join_timer() -> None:
    from cloud.peers.games import invite_color_hunt_after_join

    with (patch("cloud.peers.games.asyncio.sleep", new_callable=AsyncMock) as sleep,
          patch("cloud.peers.games.invite_color_hunt_for_activity", new_callable=AsyncMock) as invite):
        await invite_color_hunt_after_join("act_test")
        sleep.assert_awaited_once_with(10)
        invite.assert_awaited_once_with("act_test")


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
          patch("cloud.space.context.get_space_context", return_value={"peers": [
              {"unit_id": "go2", "online": True}]}),
          patch("cloud.agents.lamp.tools.execute.execute_auxiliary",
                return_value={"result": "accepted", "message_id": "invite_1"})):
        result = execute_lamp_tool("send_message", {"target": "go2",
            "content": "来玩找颜色？我亮一种颜色，你找同色物品，找到了就算你赢。", "interaction_kind": "game_invite",
            "game_id": "color_hunt"}, "call_1", agent=agent,
            unit_id="esp32_desk_led",
            context=ExecutionContext("peer_msg", "visitor", False, "main",
                                     "agent_autonomous", int(time.time() + 60)),
            dispatch=None, lighting_only=False, run_id="lamp_game")
    assert result["result"] == "accepted", result
    assert sent[-1][0] == "social_sent"


def check_lamp_sets_color_after_acceptance() -> None:
    from cloud.peers.inbound import _handle_color_hunt_acceptance

    receipts = []
    setup_calls = []
    run_status = "ok"
    message = {"message_id": "accept_1", "conversation_id": "invite_1",
               "reply_to": "invite_1", "sender": "go2", "target": "esp32_desk_led",
               "activity_id": "act_test", "deadline": time.time() + 60}
    messenger = SimpleNamespace(
        get_status=lambda _id: {"interaction_kind": "game_invite", "state": "waiting_start",
            "sender": "esp32_desk_led",
            "game_id": "color_hunt", "target": "go2", "activity_id": "act_test"},
        send_progress=lambda *args: receipts.append(("progress", args)),
        send_result=lambda *args, **kwargs: receipts.append(("result", args, kwargs)))

    def run_lamp(*args, **kwargs):
        setup_calls.append((args, kwargs))
        assert "白色" in args[0]
        dispatched = args[3]("esp32_desk_led", "SET_COLOR",
            {"r": 255, "g": 0, "b": 0, "brightness": 180},
            args[2], "lamp_semantic", task_id="task_" + "a" * 32)
        assert dispatched == "lamp_color_1"
        steps = [{"tool": "set_led_color",
            "params": {"r": 255, "g": 0, "b": 0, "brightness": 180},
            "result": {"result": "ok", "task_id": "lamp_color_1"}}]
        if run_status == "rejected":
            steps.insert(0, {"tool": "set_led_state", "params": {"state": "ON", "taught_id": "invalid"},
                             "result": {"result": "rejected", "error": "invalid_lamp_action"}})
        return {"result": run_status, "steps": steps}

    agent = SimpleNamespace(_should_act=lambda: True, _device_online=lambda: True,
                            run_user_loop=run_lamp)
    activity = SimpleNamespace(record_event=lambda *_args, **_kwargs: None)
    color = {"rgb": (255, 0, 0), "brightness": 180, "as_of": time.time() + 30}
    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.peers.games.live_lamp_color", return_value=color),
          patch("cloud.esp32.service.get_agent", return_value=agent),
          patch("cloud.api.intents._dispatch_mqtt_task", return_value="lamp_color_1"),
          patch("cloud.space.activity.get_activity_store", return_value=activity)):
        _handle_color_hunt_acceptance(messenger, message, object())
    assert setup_calls and setup_calls[0][1]["lighting_only"] is True
    assert receipts[-1] == ("result", ("accept_1", "ok"), {"task_id": "lamp_color_1"})
    receipts.clear()
    run_status = "rejected"  # Earlier invalid tool; the color step still succeeded.
    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.peers.games.live_lamp_color", return_value=color),
          patch("cloud.esp32.service.get_agent", return_value=agent),
          patch("cloud.api.intents._dispatch_mqtt_task", return_value="lamp_color_1"),
          patch("cloud.space.activity.get_activity_store", return_value=activity)):
        _handle_color_hunt_acceptance(messenger, message, object())
    assert receipts[-1] == ("result", ("accept_1", "ok"), {"task_id": "lamp_color_1"})
    receipts.clear()
    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.peers.games.live_lamp_color", return_value=None),
          patch("cloud.esp32.service.get_agent", return_value=agent),
          patch("cloud.api.intents._dispatch_mqtt_task", return_value="lamp_color_1"),
          patch("cloud.space.activity.get_activity_store", return_value=activity)):
        _handle_color_hunt_acceptance(messenger, message, object())
    assert receipts[-1][1][1] == "rejected"
    assert receipts[-1][2]["reason"] == "lamp_color_unknown_or_stale"


def check_twist_retries_before_dispatch() -> None:
    """A failed first model call can retry without sending two color commands."""
    from cloud.peers.inbound import change_color_for_game

    runs, dispatches = [], []
    color = {"rgb": (0, 0, 255), "brightness": 180, "as_of": time.time() + 1}

    def run_lamp(_goal, _unit, context, dispatch, **kwargs):
        runs.append(kwargs["run_id"])
        if len(runs) == 1:
            return {"result": "error", "steps": []}
        params = {"r": 0, "g": 0, "b": 255, "brightness": 180}
        task_id = dispatch("esp32_desk_led", "SET_COLOR", params, context,
                           "lamp_semantic", task_id="task_blue")
        return {"result": "ok", "steps": [{"tool": "set_led_color",
            "params": params, "result": {"result": "ok", "task_id": task_id}}]}

    agent = SimpleNamespace(_should_act=lambda: True, _device_online=lambda: True,
                            run_user_loop=run_lamp)

    def mqtt_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))
        return "task_blue"

    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.peers.games.live_lamp_color", return_value=color),
          patch("cloud.esp32.service.get_agent", return_value=agent),
          patch("cloud.api.intents._dispatch_mqtt_task", side_effect=mqtt_dispatch)):
        result = change_color_for_game("act_test", "invite_test", (255, 255, 255),
                                       time.time() + 45)
    assert result["result"] == "ok"
    assert runs == ["twist_invite_test", "retry_twist_invite_test"]
    assert len(dispatches) == 1


async def check_graph_handoff(fail_stage: str | None = None) -> None:
    """Confirmed target arrival is the sole trigger for the twist and return."""
    receipts, activity_events, order = [], [], []
    lamp_rgb = [(255, 255, 255)]
    drive = SimpleNamespace(
        _active_goal={"kind": "peer_message", "event_id": "game_check"},
        _context=ExecutionContext("act_test", "visitor", False, "main", "agent_autonomous"),
        _activity_id=lambda: "act_test",
        _activity_event=lambda kind, text, **facts: activity_events.append((kind, text, facts)),
        _advance_task=AsyncMock(return_value={"next_action": "run_game"}),
        user_interrupt=False)
    messenger = SimpleNamespace(
        send_result=lambda *args, **kwargs: receipts.append((args, kwargs)),
        send_message=lambda *_args, **_kwargs: {"result": "accepted"},
        get_status=lambda _message_id: {"state": "ok"})
    event = {"event_id": "game_check", "message_id": "game_check",
             "source": "esp32_desk_led", "activity_id": "act_test",
             "acceptance_message_id": "accept_game_check",
             "acceptance_sent_at": time.time(),
             "deadline": time.time() + 30,
             "message": {"sender": "esp32_desk_led", "content": "来找同色物品"},
             "messenger": messenger}

    async def graph(_session_id, _goal, **kwargs):
        from cloud.go2.perception.visual_lidar_navigate import _arrival_by_navigation
        assert _arrival_by_navigation.get() is True
        assert kwargs["publish_reply"] is False
        assert kwargs["allowed_tools"] == frozenset({
            "go2_observe", "go2_navigate_to_visible_target", "get_space_context"})
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "白色猫头鹰"})["result"] == "blocked"
        assert kwargs["tool_guard"]("go2_observe", {"question": "找白色物品"}) is None
        kwargs["on_step"]({"tool": "go2_observe", "result": {"result": "ok", "message": "看到白色猫头鹰"}})
        lamp_rgb[0] = (0, 0, 255)
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "白色猫头鹰"})["result"] == "blocked"
        lamp_rgb[0] = (255, 255, 255)
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "猫头鹰"})["result"] == "blocked"
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "白色猫头鹰"}) is None
        order.append("navigate")
        return {"result": "ok", "run_id": "game_check", "tool_results": [
            {"tool": "go2_navigate_to_visible_target", "params": {"target": "白色猫头鹰"}, "result": {
                "result": "ok", "confirmation": "odom_stopped_near_mapped_stop"}}]}

    def twist(*_args):
        order.append("recolor")
        if fail_stage == "recolor":
            return {"result": "unknown", "reason": "twist_color_unconfirmed"}
        lamp_rgb[0] = (0, 0, 255)
        return {"result": "ok", "color": {"rgb": lamp_rgb[0]}}

    async def sport(name, params, *, context):
        assert name == "go2_sport" and params in ({"cmd": "Hello"},
                                                  {"cmd": "StandDown"})
        assert context.session_id == "act_test"
        action = "hello" if params["cmd"] == "Hello" else "stand_down"
        order.append(action)
        return ({"result": "blocked", "error": "gesture_unavailable"}
                if fail_stage == action else {"result": "ok", "confirmation": "sport_rpc"})

    async def home(x, y, **_kwargs):
        assert (x, y) == (1.0, 2.0)
        order.append("return")
        return ({"status": "blocked", "reason": "no_known_free_path"}
                if fail_stage == "return" else {"status": "succeeded"})

    def off(**_kwargs):
        order.append("off")
        return ({"result": "unknown", "reason": "off_unconfirmed"}
                if fail_stage == "off" else {"result": "ok"})

    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.peers.games.live_lamp_color", side_effect=lambda _source: {
              "rgb": lamp_rgb[0], "source": "esp32_desk_led", "as_of": time.time()}),
          patch("cloud.go2.connection.go2.is_connected", True),
          patch("cloud.go2.connection.go2._odom_session_id", "odom_test", create=True),
          patch("cloud.go2.connection.sensors.Go2Sensors.odom", new_callable=PropertyMock,
                return_value={"x": 1.0, "y": 2.0}),
          patch("cloud.go2.navigation.navigator.navigator._snapshot",
                return_value=(({}, object()), None)),
          patch("cloud.go2.navigation.navigator.navigator.go_to_coordinate", home),
          patch("cloud.agents.go2.graph.run_go2_loop", graph),
          patch("cloud.peers.games._game_line", side_effect=lambda _agent, _facts, fallback: fallback),
          patch("cloud.go2.tools.executor.execute_tool", side_effect=sport),
          patch("cloud.peers.inbound.change_color_for_game", side_effect=twist),
          patch("cloud.go2.control.control_lease.acquire"),
          patch("cloud.peers.gateway.request_peer_skill", new=off),
          patch("cloud.space.activity.get_activity_store", return_value=_Activity())):
        await GAMES["color_hunt"].execute(drive, event, "peer_game_check")
    assert order == (["navigate", "hello", "recolor"] if fail_stage == "recolor" else
                     ["navigate", "hello", "recolor", "return"] if fail_stage == "return" else
                     ["navigate", "hello", "recolor", "return", "off"] if fail_stage == "off" else
                     ["navigate", "hello", "recolor", "return", "off", "stand_down"])
    assert receipts[0][0][1] == "ok"
    assert receipts[0][1]["reason"] == ("candidate_reached" if fail_stage in
                                         {"recolor", "return", "off", "stand_down"}
                                         else "story_completed")
    assert any("白色猫头鹰" in text for _kind, text, _facts in activity_events)
    assert any(kind == "action_result" and facts.get("command") == "Hello"
               and facts.get("status") == ("blocked" if fail_stage == "hello" else "ok")
               for kind, _text, facts in activity_events)


async def check_no_twist_without_arrival() -> None:
    """A plausible observation never authorizes the lamp prank or return motion."""
    receipts = []
    drive = SimpleNamespace(
        _active_goal={"event_id": "game_no_arrival"},
        _context=ExecutionContext("act_test", "visitor", False, "main", "agent_autonomous"),
        _activity_id=lambda: "act_test", _activity_event=lambda *_a, **_kw: None,
        _advance_task=AsyncMock(), user_interrupt=False)
    event = {"message_id": "game_no_arrival", "source": "esp32_desk_led",
             "activity_id": "act_test", "acceptance_message_id": "accept_no_arrival",
             "acceptance_sent_at": time.time(), "deadline": time.time() + 30,
             "message": {"sender": "esp32_desk_led", "content": "来找同色物品"},
             "messenger": SimpleNamespace(
                 get_status=lambda _id: {"state": "ok"},
                 send_result=lambda *args, **kwargs: receipts.append((args, kwargs)),
                 send_message=lambda *_a, **_kw: {"result": "accepted"})}
    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.peers.games.live_lamp_color", return_value={
              "rgb": (255, 255, 255), "source": "esp32_desk_led", "as_of": time.time()}),
          patch("cloud.go2.connection.go2.is_connected", True),
          patch("cloud.go2.connection.go2._odom_session_id", "odom_test", create=True),
          patch("cloud.go2.connection.sensors.Go2Sensors.odom", new_callable=PropertyMock,
                return_value={"x": 0.0, "y": 0.0}),
          patch("cloud.go2.navigation.navigator.navigator._snapshot",
                return_value=(({}, object()), None)),
          patch("cloud.agents.go2.graph.run_go2_loop", new_callable=AsyncMock,
                return_value={"result": "unknown", "tool_results": [{
                    "tool": "go2_observe", "result": {"result": "ok", "message": "白色猫头鹰"}}]}),
          patch("cloud.peers.inbound.change_color_for_game") as twist,
          patch("cloud.go2.navigation.navigator.navigator.go_to_coordinate") as home,
          patch("cloud.peers.gateway.request_peer_skill") as off):
        await GAMES["color_hunt"].execute(drive, event, "peer_game_no_arrival")
    twist.assert_not_called()
    home.assert_not_called()
    off.assert_not_called()
    assert receipts[0][0][1] == "unknown"


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
    asyncio.run(check_game_line_uses_lamp_role())
    asyncio.run(check_timed_activity_invite())
    asyncio.run(check_join_timer())
    check_delegated_game_handoff()
    check_lamp_sets_color_after_acceptance()
    check_twist_retries_before_dispatch()
    asyncio.run(check_graph_handoff())
    for failed_stage in ("hello", "recolor", "return", "off", "stand_down"):
        asyncio.run(check_graph_handoff(failed_stage))
    asyncio.run(check_no_twist_without_arrival())
    asyncio.run(check_game_waits_for_lamp_setup())
    print("peer game protocol checks passed")
