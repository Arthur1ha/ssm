"""Direct messages carry conversation state without declaring device success."""

import json
import threading

from cloud.peers.messaging import AgentMessenger
from cloud.space.activity import ActivityStore


class Cards:
    def __init__(self):
        self.online = {"go2": True, "esp32_desk_led": True, "cloud_orchestrator": True}

    def get_runtime_snapshot(self):
        cards = {
            unit: {"unit_id": unit, "device_id": "esp32_desk" if unit == "esp32_desk_led" else unit,
                   "agent_type": "decision" if unit == "cloud_orchestrator" else "actuator",
                   "online": online, "skills": []}
            for unit, online in self.online.items()
        }
        return cards, {card["device_id"]: 100.0 for card in cards.values()}


class Space:
    def get_space(self):
        return {"device_ids": ["go2", "esp32_desk"]}


class Bus:
    def __init__(self):
        self.messages = []

    def publish(self, topic, raw, qos=0):
        self.messages.append((topic, raw))
        return type("Info", (), {"rc": 0})()

    def deliver(self, *messengers):
        while self.messages:
            topic, raw = self.messages.pop(0)
            for messenger in messengers:
                messenger.handle_message(topic, raw)


def pair(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "test-service-token-123456")
    clock = [100.0]
    cards, space, bus = Cards(), Space(), Bus()
    got = []
    ready = threading.Event()

    def incoming(message):
        got.append(message)
        ready.set()

    go2 = AgentMessenger(bus, cards, space, {"go2"}, lambda _message: None,
                         now=lambda: clock[0])
    lamp = AgentMessenger(bus, cards, space, {"esp32_desk_led"}, incoming,
                          now=lambda: clock[0])
    return clock, cards, bus, go2, lamp, got, ready


def test_reply_progress_and_result_are_distinct(monkeypatch):
    clock, cards, bus, go2, lamp, got, ready = pair(monkeypatch)
    try:
        sent = go2.send_message("go2", "esp32_desk_led", "能调暗灯吗？")
        assert sent["result"] == "accepted"
        assert go2.get_status(sent["message_id"])["state"] == "waiting_reply"
        bus.deliver(go2, lamp)
        assert ready.wait(1)
        assert got[0]["content"] == "能调暗灯吗？"
        assert go2.get_status(sent["message_id"])["received"] is True

        reply = lamp.send_message("esp32_desk_led", "go2", "我来做", sent["message_id"],
                                  reply_kind="accepted")
        assert reply["result"] == "accepted"
        bus.deliver(go2, lamp)
        assert go2.get_status(sent["message_id"])["state"] == "waiting_start"

        assert lamp.send_progress(sent["message_id"], "lamp_task_1")["result"] == "accepted"
        bus.deliver(go2, lamp)
        assert go2.get_status(sent["message_id"])["state"] == "waiting_result"
        assert go2.get_status(sent["message_id"])["task_id"] == "lamp_task_1"

        assert lamp.send_result(sent["message_id"], "ok", task_id="lamp_task_1")["result"] == "accepted"
        bus.deliver(go2, lamp)
        assert go2.get_status(sent["message_id"])["state"] == "ok"
    finally:
        go2.close()
        lamp.close()


def test_offline_scope_replay_and_expiry(monkeypatch):
    clock, cards, bus, go2, lamp, got, ready = pair(monkeypatch)
    try:
        cards.online["esp32_desk_led"] = False
        assert go2.send_message("go2", "esp32_desk_led", "你好")["error"] == "target_offline_or_outside_space"
        assert not bus.messages
        cards.online["esp32_desk_led"] = True
        sent = go2.send_message("go2", "esp32_desk_led", "你好")
        topic, raw = bus.messages.pop(0)
        lamp.handle_message(topic, raw)
        assert ready.wait(1)
        lamp.handle_message(topic, raw)
        assert len(got) == 1
        bus.deliver(go2, lamp)
        clock[0] = 161.0
        assert go2.get_status(sent["message_id"])["state"] == "unanswered"
        assert lamp.send_message("esp32_desk_led", "go2", "迟到", sent["message_id"])["result"] == "blocked"
    finally:
        go2.close()
        lamp.close()


def test_unconfirmed_start_and_original_task_lookup(monkeypatch):
    clock, cards, bus, go2, lamp, got, ready = pair(monkeypatch)
    looked_up = []
    go2._task_result_lookup = lambda task_id: looked_up.append(task_id) or {"result": "ok"}
    try:
        first = go2.send_message("go2", "esp32_desk_led", "做一下")
        bus.deliver(go2, lamp)
        assert ready.wait(1)
        lamp.send_message("esp32_desk_led", "go2", "我来做", first["message_id"],
                          reply_kind="accepted")
        bus.deliver(go2, lamp)
        clock[0] = 161.0
        assert go2.get_status(first["message_id"])["state"] == "unconfirmed_start"
        assert looked_up == []

        clock[0] = 200.0
        second = go2.send_message("go2", "esp32_desk_led", "再做一下")
        bus.deliver(go2, lamp)
        lamp.send_progress(second["message_id"], "original_task")
        bus.deliver(go2, lamp)
        clock[0] = 261.0
        status = go2.get_status(second["message_id"])
        assert status["state"] == "ok" and looked_up == ["original_task"]
    finally:
        go2.close()
        lamp.close()


def test_invalid_sender_and_tampered_payload(monkeypatch):
    clock, cards, bus, go2, lamp, got, ready = pair(monkeypatch)
    try:
        assert go2.send_message("cloud_orchestrator", "esp32_desk_led", "伪装")["error"] == "sender_not_local"
        go2.send_message("go2", "esp32_desk_led", "你好")
        topic, raw = bus.messages.pop(0)
        payload = json.loads(raw)
        payload["content"] = "篡改"
        lamp.handle_message(topic, json.dumps(payload))
        assert not ready.wait(0.05)
    finally:
        go2.close()
        lamp.close()


def test_peer_messages_reach_visible_fallback_only_without_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "test-service-token-123456")
    store = ActivityStore(tmp_path / "activity.json")
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    visible = []
    bus = Bus()
    api = AgentMessenger(bus, Cards(), Space(), {"go2", "esp32_desk_led"},
                         lambda _message: None, on_visible_message=visible.append)
    orchestrator = AgentMessenger(bus, Cards(), Space(), {"cloud_orchestrator"},
                                  lambda _message: None)
    try:
        outside = api.send_message("go2", "esp32_desk_led", "活动外消息")
        bus.deliver(api)
        assert [item["message_id"] for item in visible] == [outside["message_id"]]
        external = orchestrator.send_message("cloud_orchestrator", "go2", "外部发来的消息")
        bus.deliver(api, orchestrator)
        assert [item["message_id"] for item in visible] == [outside["message_id"], external["message_id"]]

        source = "evt_" + "a" * 32
        store.note_user_intervention(source)
        activity = store.begin("工作", source, 1)
        inside = api.send_message("go2", "esp32_desk_led", "活动内消息")
        bus.deliver(api)
        assert len(visible) == 2
        recorded = [item for item in store.events(activity["activity_id"])
                    if item.get("message_id") == inside["message_id"]]
        assert len(recorded) == 1 and recorded[0]["text"] == "活动内消息"
    finally:
        api.close()
        orchestrator.close()
