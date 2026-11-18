"""Go2 autonomous arbitration and social workflow contracts."""

import pytest

from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.go2.workflows.free_explore import build_motivation_graph
from cloud.agents.go2.workflows.social import build_social_graph

PROFILE = {"prompt": "Go2", "revision": 7,
           "traits": {"boldness": 80, "curiosity": 70, "extraversion": 70}}


class StructuredModel:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls = []

    def with_structured_output(self, schema, *, method):
        assert method == "function_calling"
        owner = self

        class Bound:
            async def ainvoke(self, messages):
                owner.calls.append((schema.__name__, messages))
                return next(owner.decisions)

        return Bound()


@pytest.mark.asyncio
async def test_enabled_teaching_preempts_exploration_without_model():
    graph = build_motivation_graph(profile_loader=lambda: PROFILE)
    result = await graph.ainvoke({"connected": True, "curiosity": 100,
        "pending_event": {"kind": "training_event", "event_id": "owner-1",
                          "candidates": [{"teaching_id": "teach-current"}]}})
    assert result["event_decision"] == "accept"
    assert result["should_explore"] is False
    assert result["selected_teaching_id"] == "teach-current"


@pytest.mark.asyncio
async def test_peer_invitation_decided_from_fresh_context():
    model = StructuredModel([{"choice": "decline", "reason": "current task takes precedence"}])
    graph = build_motivation_graph(profile_loader=lambda: PROFILE, model=model)
    result = await graph.ainvoke({"connected": True,
        "pending_event": {"kind": "peer_message", "content": "来找灯色物品"},
        "space_summary": "灯在线", "current_goal": {"kind": "user"}})
    assert result["event_decision"] == "decline"
    assert result["should_explore"] is False
    assert model.calls[0][0] == "EventDecision"


@pytest.mark.asyncio
async def test_game_invitation_is_accepted_without_keyword_classification():
    model = StructuredModel([{"choice": "accept", "reason": "game invitation", "speech": "好呀，来玩！"}])
    graph = build_motivation_graph(profile_loader=lambda: PROFILE, model=model)
    result = await graph.ainvoke({"connected": True,
        "pending_event": {"kind": "peer_message", "interaction_kind": "game_invite",
                          "game_id": "color_hunt", "content": "我是什么颜色你就找什么"}})
    assert result["event_decision"] == "accept"
    assert result["next_action"] == "run_peer_task"


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

    async def send(target, content, **metadata):
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

    async def send(target, content, **metadata):
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


@pytest.mark.asyncio
async def test_game_checkpoint_delegates_steps_to_existing_agent_graph():
    graph = build_motivation_graph(profile_loader=lambda: PROFILE)
    goal = {"kind": "game", "event_id": "color-1", "deadline": 1000}
    started = await graph.ainvoke({"connected": True, "current_goal": goal,
        "task_update": {"event_id": "color-1", "stage": "start"}})
    assert started["task_stage"] == "running"
    assert started["next_action"] == "run_game"
    finished = await graph.ainvoke({**started, "task_update": {"event_id": "color-1",
        "stage": "finish", "result": {"result": "unknown"}}})
    assert finished["task_stage"] == "finished"
    assert finished["next_action"] == "none"
