"""Go2 social Graph keeps delivery distinct from peer acceptance."""

import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.go2.workflows.social import build_social_graph


PROFILE = {"prompt": "Go2", "traits": {"extraversion": 75}}


class Model:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def with_structured_output(self, schema, *, method):
        assert schema.__name__ == "SocialDecision"
        assert method == "function_calling"
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        return self.decision


def state(**overrides):
    return {"run_id": uuid.uuid4().hex,
            "activity_id": "act-1", "activity": {"kind": "explore"},
            "opportunity": {"event_ids": ["discovery-1"], "kind": "own_observation"},
            "peers": [{"unit_id": "esp32_desk_led", "name": "灯", "skills": []}],
            "space_summary": "客厅", "active": True, "connected": True,
            "interrupted": False, "pending_question": False,
            "seen_event_ids": [], **overrides}


@pytest.mark.asyncio
async def test_accepted_delivery_does_not_claim_peer_finished(tmp_path):
    model = Model({"decision": "send_message", "target": "esp32_desk_led",
                   "content": "我看到了门口有人", "reason": "共享发现"})
    sent = []

    async def send(target, content):
        sent.append((target, content))
        return {"result": "accepted", "message_id": "msg-1"}

    graph = build_social_graph(model, profile_loader=lambda: PROFILE,
                               can_send=lambda activity, target, reason: True,
                               send_message=send, checkpointer=InMemorySaver(),
                               effect_ledger=FileEffectLedger(tmp_path / "effects"))
    result = await graph.ainvoke(state(), {"configurable": {"thread_id": "go2-social-1"}})
    assert result["status"] == "accepted"
    assert result["response"] == "消息已发送，尚未确认同伴回复"
    assert result["seen_event_ids"] == ["discovery-1"]
    assert sent == [("esp32_desk_led", "我看到了门口有人")]


@pytest.mark.asyncio
async def test_live_activity_or_budget_change_blocks_delivery(tmp_path):
    model = Model({"decision": "send_message", "target": "esp32_desk_led",
                   "content": "发现", "reason": "共享发现"})
    sent = []

    async def send(target, content):
        sent.append(target)
        return {"result": "accepted"}

    graph = build_social_graph(model, profile_loader=lambda: PROFILE,
                               can_send=lambda activity, target, reason: False,
                               send_message=send,
                               effect_ledger=FileEffectLedger(tmp_path / "effects"))
    result = await graph.ainvoke(state())
    assert result["status"] == "blocked"
    assert sent == []


@pytest.mark.asyncio
async def test_unknown_peer_and_repeated_discovery_do_not_send(tmp_path):
    model = Model({"decision": "send_message", "target": "unknown",
                   "content": "hello", "reason": "test"})
    sent = []

    async def send(target, content):
        sent.append(target)
        return {"result": "accepted"}

    graph = build_social_graph(model, profile_loader=lambda: PROFILE,
                               can_send=lambda activity, target, reason: True,
                               send_message=send,
                               effect_ledger=FileEffectLedger(tmp_path / "effects"))
    rejected = await graph.ainvoke(state())
    assert rejected["status"] == "rejected"
    assert sent == []

    skipped_model = Model({"decision": "send_message", "target": "esp32_desk_led",
                           "content": "hello"})
    skipped_graph = build_social_graph(skipped_model, profile_loader=lambda: PROFILE,
                                      can_send=lambda *_args: True, send_message=send,
                                      effect_ledger=FileEffectLedger(tmp_path / "effects"))
    skipped = await skipped_graph.ainvoke(state(seen_event_ids=["discovery-1"]))
    assert skipped["status"] == "skipped"
    assert skipped_model.calls == 0


@pytest.mark.asyncio
async def test_repeated_social_run_does_not_send_duplicate_message(tmp_path):
    model = Model({"decision": "send_message", "target": "esp32_desk_led",
                   "content": "发现", "reason": "共享发现"})
    sent = []

    async def send(target, content):
        sent.append((target, content))
        return {"result": "accepted", "message_id": "msg-once"}

    graph = build_social_graph(model, profile_loader=lambda: PROFILE,
                               can_send=lambda *_args: True, send_message=send,
                               effect_ledger=FileEffectLedger(tmp_path / "effects"))
    initial = state(run_id="social-replay")
    first = await graph.ainvoke(initial)
    second = await graph.ainvoke(initial)
    assert first["delivery"] == second["delivery"]
    assert sent == [("esp32_desk_led", "发现")]
