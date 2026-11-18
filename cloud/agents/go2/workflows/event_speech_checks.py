"""Arbitration facts and an optional peer response share one decision call."""

import pytest

from cloud.agents.go2.workflows.free_explore import build_motivation_graph


@pytest.mark.parametrize("invitation,speech,expected", [
    (False, "", "accept"),
    (True, "好呀，来玩！", "accept"),
    (True, "", "defer"),
])
async def test_peer_arbitration_separates_reason_from_speech(invitation, speech, expected):
    class Model:
        calls = 0

        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            self.calls += 1
            return {"choice": "accept", "reason": "当前活动和空闲状态允许接受。", "speech": speech}

    model = Model()
    graph = build_motivation_graph(profile_loader=lambda: {
        "revision": 1, "traits": {}, "prompt": "自然说话"}, model=model)
    result = await graph.ainvoke({"connected": True, "interrupted": False,
        "pending_event": {"kind": "peer_message", "event_id": "invitation",
            "interaction_kind": "game_invite" if invitation else "message",
            "content": "一起玩吧" if invitation else "你那边怎么样？"}})
    assert model.calls == 1
    assert result["event_decision"] == expected
    assert result["decision_speech"] == speech
    if expected == "accept":
        assert result["decision_reason"] == "当前活动和空闲状态允许接受。"
        assert result["decision_speech"] != result["decision_reason"]
    else:
        assert result["next_action"] == "none"
