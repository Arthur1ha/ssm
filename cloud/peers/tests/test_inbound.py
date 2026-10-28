"""Peer promises do not become physical completion claims."""

import time
from types import SimpleNamespace

from cloud.peers.inbound import _finish, _publish_go2_reply_ack, handle_api_peer_message
from cloud.space.activity import ActivityStore


class Messenger:
    def __init__(self):
        self.calls = []

    def send_progress(self, *args):
        self.calls.append(("progress", args))

    def send_message(self, *args, **kwargs):
        self.calls.append(("message", args, kwargs))

    def send_result(self, *args, **kwargs):
        self.calls.append(("result", args, kwargs))

    def get_status(self, _message_id):
        return {"state": "waiting_result", "task_id": "physical_1"}


MESSAGE = {"kind": "message", "message_id": "msg_1", "conversation_id": "conv_1",
           "sender": "go2", "target": "esp32_desk_led", "content": "帮我调灯",
           "deadline": time.time() + 60}


def test_uncertain_agent_result_does_not_report_success():
    messenger = Messenger()
    _finish(messenger, MESSAGE, {"result": "accepted", "reply": "我来做",
                                 "task_id": "physical_1"})
    assert [item[0] for item in messenger.calls] == ["progress", "message"]
    assert messenger.calls[0][1] == ("msg_1", "physical_1")


def test_confirmed_agent_result_reports_original_task():
    messenger = Messenger()
    _finish(messenger, MESSAGE, {"result": "ok", "reply": "完成了",
                                 "task_id": "physical_1"})
    assert messenger.calls[-2] == ("result", ("msg_1", "ok"), {"task_id": "physical_1"})


def test_expired_peer_message_never_starts_agent(monkeypatch):
    messenger = Messenger()
    expired = {**MESSAGE, "deadline": time.time() - 1}
    handle_api_peer_message(messenger, expired, None)
    assert messenger.calls == [("result", ("msg_1", "cancelled"),
                                {"reason": "message_expired"})]


def test_reply_is_recorded_but_never_starts_a_new_agent_turn():
    messenger = Messenger()
    handle_api_peer_message(messenger, {**MESSAGE, "reply_to": "msg_previous"}, None)
    assert messenger.calls == []


def test_go2_peer_reply_creates_one_local_bubble_without_sending_again(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path / "activity.json")
    store.note_user_intervention("evt_test")
    activity = store.begin("我要工作了", "evt_test", 1)
    store.set_device(activity["activity_id"], "go2", mode="free_explore", status="joined")
    store.set_device(activity["activity_id"], "esp32_desk_led", mode="reactive", status="joined")
    store.activate(activity["activity_id"])
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    monkeypatch.setattr("cloud.go2.agentcore.soul.load_system_prompt",
                        lambda: ("我是机器狗", "test", None))
    class Model:
        async def ainvoke(self, _messages):
            return SimpleNamespace(content="灯的回复收到了，我继续找能走的路。")
    monkeypatch.setattr("cloud.go2.agentcore.llm.get_text_llm", lambda: Model())
    messenger = Messenger()
    reply = {**MESSAGE, "sender": "esp32_desk_led", "target": "go2",
             "content": "灯光已调好", "reply_to": "msg_original",
             "activity_id": activity["activity_id"], "reply_kind": "reply"}
    _publish_go2_reply_ack(messenger, reply)
    rows = [event for event in store.events(activity["activity_id"])
            if event.get("source") == "peer_reply_ack"]
    assert len(rows) == 1
    assert rows[0]["unit_id"] == "go2"
    assert rows[0]["reply_to"] == reply["message_id"]
    assert messenger.calls == []


def test_ended_activity_message_does_not_start_agent(monkeypatch):
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: type(
        "Store", (), {"current": lambda self: {"activity_id": "act_1", "status": "ending",
                                                "devices": {"go2": {"status": "joined"},
                                                            "esp32_desk_led": {"status": "joined"}}}})())
    messenger = Messenger()
    handle_api_peer_message(messenger, {**MESSAGE, "activity_id": "act_1"}, None)
    assert messenger.calls == [("result", ("msg_1", "cancelled"),
                                {"reason": "activity_ended"})]
