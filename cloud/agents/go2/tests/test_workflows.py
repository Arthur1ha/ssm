"""Isolated Go2 autonomy Graph policy and factual reply contracts."""

import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.go2.workflows.free_explore import build_motivation_graph
from cloud.agents.go2.workflows.reactive import build_action_graph
from cloud.agents.go2.workflows.social import build_social_graph


PROFILE = {"prompt": "Go2", "revision": 7,
           "traits": {"boldness": 80, "curiosity": 70, "extraversion": 70}}


class StructuredModel:
    def __init__(self, decisions, replies=()):
        self.decisions = iter(decisions)
        self.replies = iter(replies)
        self.calls = []

    def with_structured_output(self, schema, *, method):
        assert method == "function_calling"
        owner = self

        class Bound:
            async def ainvoke(self, messages):
                owner.calls.append((schema.__name__, messages))
                source = (owner.decisions if schema.__name__ in
                          {"ActionDecision", "SocialDecision"} else owner.replies)
                return next(source)

        return Bound()


def state(**overrides):
    return {"run_id": uuid.uuid4().hex,
            "kind": "reactive", "scene": "看见一个人", "identity": "未确认",
            "memory_summary": "", "space_summary": "", "active": True,
            "connected": True, "interrupted": False, "now": 100.0,
            "last_autonomous_ts": 0.0, "action_cooldowns": {}, **overrides}


@pytest.mark.asyncio
async def test_reactive_graph_observes_receipt_then_narrates(tmp_path):
    model = StructuredModel([{"action": "Hello", "reason": "友好招呼"}],
                            [{"text": "汪，招呼打完啦！"}])
    dispatched = []

    async def execute(action, reason):
        dispatched.append((action, reason))
        return {"result": "ok", "receipt": "webrtc"}

    graph = build_action_graph(model, kind="reactive", profile_loader=lambda: PROFILE,
                               execute=execute, can_dispatch=lambda: True,
                               checkpointer=InMemorySaver(),
                               effect_ledger=FileEffectLedger(tmp_path / "effects"))
    result = await graph.ainvoke(state(), {"configurable": {"thread_id": "go2-auto-1"}})
    assert result["status"] == "ok"
    assert result["response"] == "汪，招呼打完啦！"
    assert result["result"]["receipt"] == "webrtc"
    assert result["last_autonomous_ts"] == 100.0
    assert result["action_cooldowns"]["Hello"] == 100.0
    assert dispatched == [("Hello", "友好招呼")]
    assert [schema for schema, _ in model.calls] == ["ActionDecision", "AutonomousReply"]


@pytest.mark.asyncio
async def test_uncertain_result_cannot_be_narrated_as_completed(tmp_path):
    model = StructuredModel([{"action": "Hello", "reason": "招呼"}],
                            [{"text": "我已经完成了！"}])

    async def execute(_action, _reason):
        return {"result": "accepted"}

    graph = build_action_graph(model, kind="social", profile_loader=lambda: PROFILE,
                               execute=execute, can_dispatch=lambda: True,
                               effect_ledger=FileEffectLedger(tmp_path / "effects"))
    result = await graph.ainvoke(state(kind="social"))
    assert result["status"] == "accepted"
    assert "没收到动作完成的确认" in result["response"]


@pytest.mark.asyncio
async def test_interruption_and_trait_candidates_block_action():
    model = StructuredModel([{"action": "turn_left", "reason": "想转头"}])
    actions = []

    async def execute(action, _reason):
        actions.append(action)
        return {"result": "ok"}

    graph = build_action_graph(model, kind="reactive",
                               profile_loader=lambda: {**PROFILE, "traits": {**PROFILE["traits"], "boldness": 10}},
                               execute=execute, can_dispatch=lambda: True)
    rejected = await graph.ainvoke(state())
    assert rejected["status"] == "rejected"
    assert actions == []
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_interruption_blocks_model_and_action():
    model = StructuredModel([])
    actions = []

    async def execute(action, _reason):
        actions.append(action)
        return {"result": "ok"}

    graph = build_action_graph(model, kind="reactive", profile_loader=lambda: PROFILE,
                               execute=execute, can_dispatch=lambda: True)
    cancelled = await graph.ainvoke(state(interrupted=True))
    assert cancelled["status"] == "cancelled"
    assert actions == []
    assert model.calls == []


@pytest.mark.asyncio
async def test_live_guard_rechecks_before_dispatch():
    model = StructuredModel([{"action": "Hello", "reason": "招呼"}])
    actions = []

    async def execute(action, _reason):
        actions.append(action)
        return {"result": "ok"}

    graph = build_action_graph(model, kind="reactive", profile_loader=lambda: PROFILE,
                               execute=execute, can_dispatch=lambda: False)
    result = await graph.ainvoke(state())
    assert result["status"] == "cancelled"
    assert actions == []


@pytest.mark.asyncio
async def test_action_replay_uses_original_receipt_without_redispatch(tmp_path):
    model = StructuredModel([{"action": "Hello", "reason": "招呼"},
                             {"action": "Hello", "reason": "招呼"}],
                            [{"text": "汪，招呼打完啦！"},
                             {"text": "汪，招呼打完啦！"}])
    calls = []

    async def execute(action, reason):
        calls.append((action, reason))
        return {"result": "ok", "receipt": "webrtc"}

    graph = build_action_graph(model, kind="reactive", profile_loader=lambda: PROFILE,
                               execute=execute, can_dispatch=lambda: True,
                               effect_ledger=FileEffectLedger(tmp_path / "effects"))
    original = state(run_id="go2-autonomy-replay")
    first = await graph.ainvoke(original)
    second = await graph.ainvoke(original)
    assert first["status"] == second["status"] == "ok"
    assert calls == [("Hello", "招呼")]


@pytest.mark.asyncio
async def test_action_replay_without_receipt_stays_unknown(tmp_path):
    model = StructuredModel([{"action": "Hello", "reason": "招呼"},
                             {"action": "Hello", "reason": "招呼"}],
                            [{"text": "还没确认"}, {"text": "还没确认"}])
    calls = []

    async def execute(action, _reason):
        calls.append(action)
        return {"result": "unknown"}

    graph = build_action_graph(model, kind="reactive", profile_loader=lambda: PROFILE,
                               execute=execute, can_dispatch=lambda: True,
                               effect_ledger=FileEffectLedger(tmp_path / "effects"))
    original = state(run_id="go2-autonomy-unknown")
    first = await graph.ainvoke(original)
    second = await graph.ainvoke(original)
    assert first["status"] == second["status"] == "unknown"
    assert calls == ["Hello"]


@pytest.mark.asyncio
async def test_invalid_structured_decision_never_dispatches():
    model = StructuredModel([{"action": ["Hello", "Dance1"], "reason": "bad"}])
    actions = []

    async def execute(action, _reason):
        actions.append(action)
        return {"result": "ok"}

    graph = build_action_graph(model, kind="reactive", profile_loader=lambda: PROFILE,
                               execute=execute, can_dispatch=lambda: True)
    result = await graph.ainvoke(state())
    assert result["status"] == "error"
    assert actions == []


@pytest.mark.asyncio
async def test_free_explore_motivation_graph_matches_curiosity_and_social_ticks():
    graph = build_motivation_graph(profile_loader=lambda: PROFILE)
    exploring = await graph.ainvoke({"state": "IDLE", "curiosity": 9, "social_tick": 1,
                                     "connected": True, "person_engaging": False})
    assert exploring["state"] == "EXPLORING"
    assert exploring["should_explore"] is True
    assert exploring["curiosity"] == 0
    social = await graph.ainvoke({"state": "IDLE", "curiosity": 0, "social_tick": 4,
                                  "connected": True, "person_engaging": True})
    assert social["state"] == "IDLE"
    assert social["should_social"] is True
    paused = await graph.ainvoke({"state": "IDLE", "curiosity": 9, "social_tick": 4,
                                  "connected": True, "person_engaging": True,
                                  "interrupted": True})
    assert paused["should_explore"] is False
    assert paused["should_social"] is False
    assert paused["curiosity"] == 9


@pytest.mark.asyncio
async def test_social_graph_sends_only_to_current_peer(tmp_path):
    model = StructuredModel([{"decision": "send_message", "target": "esp32_desk_led",
                              "content": "看见人走近了", "reason": "可靠观察"}])
    sent = []

    async def send(target, content):
        sent.append((target, content))
        return {"result": "accepted", "message_id": "msg-1"}

    graph = build_social_graph(model, profile_loader=lambda: PROFILE,
                               can_send=lambda *_args: True, send_message=send,
                               effect_ledger=FileEffectLedger(tmp_path / "effects"))
    result = await graph.ainvoke({
        "run_id": "social-accepted",
        "activity_id": "activity-1", "activity": {"status": "active"},
        "opportunity": {"event_ids": ["seen-1"]},
        "peers": [{"unit_id": "esp32_desk_led", "name": "灯"}],
        "active": True, "connected": True,
    })
    assert result["status"] == "accepted"
    assert result["delivery"]["message_id"] == "msg-1"
    assert sent == [("esp32_desk_led", "看见人走近了")]


@pytest.mark.asyncio
async def test_social_graph_rejects_model_selected_unknown_peer(tmp_path):
    model = StructuredModel([{"decision": "send_message", "target": "unknown",
                              "content": "你好"}])
    sent = []

    async def send(target, content):
        sent.append((target, content))
        return {"result": "accepted"}

    graph = build_social_graph(model, profile_loader=lambda: PROFILE,
                               can_send=lambda *_args: True, send_message=send,
                               effect_ledger=FileEffectLedger(tmp_path / "effects"))
    result = await graph.ainvoke({
        "run_id": "social-unknown-peer",
        "activity_id": "activity-1", "opportunity": {"event_ids": ["seen-1"]},
        "peers": [{"unit_id": "esp32_desk_led"}], "active": True, "connected": True,
    })
    assert result["status"] == "rejected"
    assert sent == []
