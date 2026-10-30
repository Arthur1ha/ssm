import asyncio
import json
import time
from types import SimpleNamespace

from cloud.go2.agentcore.mode import free_explore
from cloud.go2.agentcore.perception.identity import IdentityEvent, IdentitySnapshot


def test_drive_social_join_sends_once_and_skip_consumes_opportunity(monkeypatch):
    drive = free_explore.Drive()
    activity = {"activity_id": "act_test", "started_at": time.time(), "status": "active"}
    peer = {"unit_id": "lamp", "name": "灯", "online": True, "skills": [{"id": "adjust_lighting"}]}
    context = {"activity": activity, "peers": [peer], "recent_discoveries": []}
    drive._social_state._born_at = activity["started_at"] - 1
    monkeypatch.setattr(drive, "_activity_id", lambda: "act_test")
    monkeypatch.setattr(free_explore.go2, "is_connected", True)
    monkeypatch.setattr(free_explore, "activity_peers", lambda _unit, _peers: (activity, [peer]))
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *a, **k: context)
    monkeypatch.setattr(free_explore, "get_profile",
                        lambda: {"prompt": "简短说话", "traits": {"extraversion": 80}})
    monkeypatch.setattr(free_explore, "prompt_space_context", lambda: "{}")
    sent = []
    monkeypatch.setattr("cloud.go2.agentcore.tools.tools.send_message",
                        lambda target, content: sent.append((target, content)) or
                        {"result": "accepted", "message_id": "msg_1", "conversation_id": "conv_1"})

    class Model:
        decision = {"decision": "send_message", "target": "lamp", "content": "主人在工作，保持稳定照明好吗？", "reason": "工作照明"}

        async def ainvoke(self, _messages):
            return SimpleNamespace(content=json.dumps(self.decision))

    model = Model()
    monkeypatch.setattr(free_explore, "get_text_llm", lambda: model)
    asyncio.run(drive._evaluate_social())
    asyncio.run(drive._evaluate_social())
    assert sent == [("lamp", "主人在工作，保持稳定照明好吗？")]
    assert len(drive._social_state.sent_times) == 1

    model.decision = {"decision": "skip", "reason": "正在睡觉，不打扰"}
    discovery = {"event_id": "evt_new", "event_type": "observation", "summary": "有人睡着了"}
    context["recent_discoveries"] = [discovery]
    asyncio.run(drive._evaluate_social())
    asyncio.run(drive._evaluate_social())
    assert sent == [("lamp", "主人在工作，保持稳定照明好吗？")]
    assert "evt_new" in drive._social_state.seen_discoveries


def test_only_identity_recognized_owner_becomes_owner_discovery(monkeypatch):
    from cloud.go2 import router

    recorded = []
    store = SimpleNamespace(
        current=lambda: {"activity_id": "act_test", "status": "active",
                         "devices": {"go2": {"status": "joined"}}},
        record_event=lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    monkeypatch.setattr(router.reactive_mind, "on_identity_event", lambda _event: None)
    monkeypatch.setattr(router.drive, "on_identity_event", lambda _event: None)
    idle = IdentitySnapshot(status="idle")
    router._on_identity_event(IdentityEvent(idle, IdentitySnapshot(
        status="recognized", person_id="guest", name="客人", role="guest", score=96)))
    assert recorded == []
    router._on_identity_event(IdentityEvent(idle, IdentitySnapshot(
        status="recognized", person_id="owner", name="阿明", role="owner", score=95)))
    assert recorded[0][0] == ("act_test", "person_identified")
    assert recorded[0][1]["confidence"] == 95
