"""Peer promises do not become physical completion claims."""

import time

from cloud.peers.inbound import _finish, handle_api_peer_message


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


def test_ended_activity_message_does_not_start_agent(monkeypatch):
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: type(
        "Store", (), {"current": lambda self: {"activity_id": "act_1", "status": "ending",
                                                "devices": {"go2": {"status": "joined"},
                                                            "esp32_desk_led": {"status": "joined"}}}})())
    messenger = Messenger()
    handle_api_peer_message(messenger, {**MESSAGE, "activity_id": "act_1"}, None)
    assert messenger.calls == [("result", ("msg_1", "cancelled"),
                                {"reason": "activity_ended"})]
