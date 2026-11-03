"""Offline peer game protocol checks; run with ``uv run python -m cloud.peers.games_checks``."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

from cloud.mqtt_signing import sign_message
from cloud.peers.games import Game, playable_games
from cloud.peers.messaging import AgentMessenger


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


if __name__ == "__main__":
    check()
    print("peer game protocol checks passed")
