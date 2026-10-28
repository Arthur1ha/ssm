import time

from cloud.esp32.agent import ESP32Agent


class _State:
    def actuator_snapshot(self):
        return {"esp32_desk_led": {"state": {"ism": "ON"}}}


def test_lamp_send_message_obeys_activity_mode_and_budget(monkeypatch):
    agent = ESP32Agent(_State(), llm=object())
    activity = {"activity_id": "act_test", "status": "active",
                "devices": {"esp32_desk_led": {"status": "joined"},
                            "go2": {"status": "joined"}}}
    peer = {"unit_id": "go2", "online": True}
    monkeypatch.setattr("cloud.space.context.get_space_context",
                        lambda *a, **k: {"peers": [peer]})
    monkeypatch.setattr("cloud.peers.social.activity_peers",
                        lambda _unit, _peers: (activity, [peer]))
    calls = []

    def auxiliary(**kwargs):
        calls.append(kwargs)
        return ({"result": "accepted", "message_id": "msg_1"}
                if kwargs["mode_available"] else {"result": "blocked"})

    monkeypatch.setattr("cloud.esp32.agentcore.tools.executor.execute_auxiliary", auxiliary)
    agent._social_state.reset("act_test", joined_at=time.time())
    params = {"target": "go2", "content": "工作中保持稳定照明好吗？"}
    assert not agent._dispatch_tool("send_message", params, "esp32_desk_led")
    agent.set_autonomy_mode("reactive")
    assert agent._dispatch_tool("send_message", params, "esp32_desk_led")
    assert not agent._dispatch_tool("send_message", params, "esp32_desk_led")
    assert len(agent._social_state.sent_times) == 1
    assert calls[-1]["params"] == params

    agent._social_state.last_sent = time.time() - 301
    agent._social_state.sent_times.clear()
    agent._user_hold_until = time.time() + 60
    assert not agent._dispatch_tool("send_message", {**params, "content": "另一个话题"}, "esp32_desk_led")


def test_lamp_normalizes_model_message_aliases_before_validated_dispatch(monkeypatch):
    agent = ESP32Agent(_State(), llm=object())
    agent.set_autonomy_mode("reactive")
    activity = {"activity_id": "act_test", "status": "active",
                "devices": {"esp32_desk_led": {"status": "joined"},
                            "go2": {"status": "joined"}}}
    peer = {"unit_id": "go2", "online": True}
    monkeypatch.setattr("cloud.space.context.get_space_context",
                        lambda *a, **k: {"peers": [peer]})
    monkeypatch.setattr("cloud.peers.social.activity_peers",
                        lambda _unit, _peers: (activity, [peer]))
    calls = []
    monkeypatch.setattr("cloud.esp32.agentcore.tools.executor.execute_auxiliary",
                        lambda **kwargs: calls.append(kwargs) or
                        {"result": "accepted", "message_id": "msg_1"})
    agent._social_state.reset("act_test", joined_at=time.time())

    assert agent._dispatch_tool("send_message", {
        "unit_id": "go2", "message": "我看到你在检查通路，需要灯光配合就告诉我。",
    }, "esp32_desk_led")
    assert calls[0]["params"] == {
        "target": "go2", "content": "我看到你在检查通路，需要灯光配合就告诉我。"}
