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
            messenger._workers.shutdown(wait=True)
            assert len(delivered) == 1 and delivered[0]["game_id"] == "color_hunt"
            assert messenger.handle_message(topic, raw) and len(delivered) == 1
            payload = json.loads(raw)
            payload.pop("_mqtt_auth")
            payload["game_id"] = "unknown"
            assert messenger.handle_message(topic, json.dumps(sign_message(topic, payload)))
            assert len(delivered) == 1
            payload["game_id"] = "color_hunt"
            payload["deadline"] = time.time() - 1
            assert messenger.handle_message(topic, json.dumps(sign_message(topic, payload)))
            assert len(delivered) == 1
            payload["deadline"] = time.time() + 30
            payload["message_id"] = payload["conversation_id"] = "msg_cross_activity"
            store.value["activity_id"] = "act_other"
            assert messenger.handle_message(topic, json.dumps(sign_message(topic, payload)))
            assert len(delivered) == 1
            store.value["activity_id"] = "act_test"
            store.value["devices"]["go2"]["status"] = "left"
            assert playable_games("act_test", "esp32_desk_led", "go2") == []
            assert messenger.send_message("esp32_desk_led", "go2", "来玩",
                interaction_kind="game_invite", game_id="color_hunt")["result"] == "blocked"
            store.value = None
            assert messenger.send_message("esp32_desk_led", "go2", "普通消息")["result"] == "accepted"
        finally:
            messenger.close()


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
        send_message=lambda *_args, **_kwargs: None)
    event = {"event_id": "game_check", "message_id": "game_check",
             "source": "esp32_desk_led", "activity_id": "act_test",
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
          patch("cloud.agents.go2.graph.run_go2_loop", graph)):
        await GAMES["color_hunt"].execute(drive, event, "peer_game_check")
    assert receipts[0][0][1] == "unknown"
    assert receipts[0][1]["reason"] == "color_match_unverified"


if __name__ == "__main__":
    check()
    asyncio.run(check_graph_handoff())
    print("peer game protocol checks passed")
