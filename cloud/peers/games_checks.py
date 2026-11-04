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
        self.value = {"activity_id": "act_test", "status": "active", "devices": {
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
    game = Game("color_hunt", "寻找同色物品", "按实时灯色寻找物品",
                ("esp32_desk_led", "go2"), lambda _sender, _target: True,
                lambda *_args: None)
    with (patch("cloud.space.activity.get_activity_store", return_value=store),
          patch.dict("cloud.peers.games.GAMES", {"color_hunt": game}, clear=True)):
        messenger = AgentMessenger(mqtt, None, None,
                                   {"esp32_desk_led", "go2"}, delivered.append)
        messenger._allowed = lambda _sender, _target: True
        try:
            assert [item["game_id"] for item in playable_games(
                "act_test", "esp32_desk_led", "go2")] == ["color_hunt"]
            assert playable_games("act_test", "go2", "esp32_desk_led") == []
            sent = messenger.send_message("esp32_desk_led", "go2", "找一找同色物品",
                interaction_kind="game_invite", game_id="color_hunt")
            assert sent["result"] == "accepted"
            assert 200 < messenger.get_status(sent["message_id"])["deadline"] - time.time() <= 241
            topic, raw = mqtt.sent[-1]
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
          patch("cloud.space.context.get_space_context", return_value={"peers": [
              {"unit_id": "go2", "online": True}]}),
          patch("cloud.agents.lamp.tools.execute.execute_auxiliary",
                return_value={"result": "accepted", "message_id": "invite_1"})):
        result = execute_lamp_tool("send_message", {"target": "go2",
            "content": "一起找同色物品吧", "interaction_kind": "game_invite",
            "game_id": "color_hunt"}, "call_1", agent=agent,
            unit_id="esp32_desk_led",
            context=ExecutionContext("peer_msg", "visitor", False, "main",
                                     "agent_autonomous", int(time.time() + 60)),
            dispatch=None, lighting_only=False, run_id="lamp_game")
    assert result["result"] == "accepted"
    assert sent[-1][0] == "social_sent"


def check_lamp_sets_color_after_acceptance() -> None:
    from cloud.peers.inbound import _handle_color_hunt_acceptance

    receipts = []
    setup_calls = []
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
        dispatched = args[3]("esp32_desk_led", "SET_COLOR",
            {"r": 255, "g": 0, "b": 0, "brightness": 180},
            args[2], "lamp_semantic", task_id="task_" + "a" * 32)
        assert dispatched == "lamp_color_1"
        return {"result": "ok", "steps": [{"tool": "set_led_color",
            "params": {"r": 255, "g": 0, "b": 0, "brightness": 180},
            "result": {"result": "ok", "task_id": "lamp_color_1"}}]}

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
    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.peers.games.live_lamp_color", return_value=None),
          patch("cloud.esp32.service.get_agent", return_value=agent),
          patch("cloud.api.intents._dispatch_mqtt_task", return_value="lamp_color_1"),
          patch("cloud.space.activity.get_activity_store", return_value=activity)):
        _handle_color_hunt_acceptance(messenger, message, object())
    assert receipts[-1][1][1] == "rejected"
    assert receipts[-1][2]["reason"] == "lamp_color_unknown_or_stale"


async def check_graph_handoff() -> None:
    """The game supplies facts and gates; Go2's Graph chooses the actual tools."""
    receipts = []
    lamp_rgb = [(255, 0, 0)]
    drive = SimpleNamespace(
        _active_goal={"kind": "peer_message", "event_id": "game_check"},
        _context=ExecutionContext("act_test", "visitor", False, "main", "agent_autonomous"),
        _activity_id=lambda: "act_test", _activity_event=lambda *_a, **_kw: None,
        _advance_task=AsyncMock(return_value={"next_action": "run_game"}),
        user_interrupt=False)
    messenger = SimpleNamespace(
        send_result=lambda *args, **kwargs: receipts.append((args, kwargs)),
        send_message=lambda *_args, **_kwargs: None,
        get_status=lambda _message_id: {"state": "ok"})
    event = {"event_id": "game_check", "message_id": "game_check",
             "source": "esp32_desk_led", "activity_id": "act_test",
             "acceptance_message_id": "accept_game_check",
             "acceptance_sent_at": time.time(),
             "deadline": time.time() + 30,
             "message": {"sender": "esp32_desk_led", "content": "来找同色物品"},
             "messenger": messenger}

    async def graph(_session_id, _goal, **kwargs):
        assert kwargs["publish_reply"] is False
        assert kwargs["allowed_tools"] == frozenset({
            "go2_observe", "go2_navigate_to_visible_target", "get_space_context"})
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "红杯子"})["result"] == "blocked"
        assert kwargs["tool_guard"]("go2_observe", {"question": "找红色物品"}) is None
        kwargs["on_step"]({"tool": "go2_observe", "result": {"result": "ok", "message": "看到红杯子"}})
        lamp_rgb[0] = (0, 0, 255)
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "红杯子"})["result"] == "blocked"
        lamp_rgb[0] = (255, 0, 0)
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {"target": "红杯子"}) is None
        return {"result": "ok", "run_id": "game_check", "tool_results": [
            {"tool": "go2_navigate_to_visible_target", "result": {
                "result": "ok", "confirmation": "odom_stopped_and_rgb_lidar_reobserved"}}]}

    with (patch("cloud.peers.games.activity_allows", return_value=True),
          patch("cloud.peers.games.live_lamp_color", side_effect=lambda _source: {
              "rgb": lamp_rgb[0], "source": "esp32_desk_led", "as_of": time.time()}),
          patch("cloud.go2.connection.go2.is_connected", True),
          patch("cloud.go2.navigation.navigator.navigator._snapshot",
                return_value=(({}, object()), None)),
          patch("cloud.agents.go2.graph.run_go2_loop", graph)):
        await GAMES["color_hunt"].execute(drive, event, "peer_game_check")
    assert receipts[0][0][1] == "unknown"
    assert receipts[0][1]["reason"] == "color_match_unverified"


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
    check_delegated_game_handoff()
    check_lamp_sets_color_after_acceptance()
    asyncio.run(check_graph_handoff())
    asyncio.run(check_game_waits_for_lamp_setup())
    print("peer game protocol checks passed")
