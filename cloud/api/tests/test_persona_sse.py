"""The browser sees only scoped, replayable facts and final persona replies."""

import queue
import time
import json
import threading

import pytest

from cloud.agents.common.runner import GraphRun as Run
from cloud.agents.common.replies import persona_reply_prompt
from cloud.agents.common.progress import submit_progress
from cloud.agents.common.progress_facts import step_progress_facts
from cloud.api import intents
from cloud.agents.go2.personality.behavior import reply_persona


def _session(session_id, space_id="main"):
    return {"session_id": session_id, "space_id": space_id,
            "actor": "admin", "expires_at": time.time() + 60}


def test_private_reply_isolated_deduplicated_and_replayed(monkeypatch):
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, "_events", broker)
    left = broker.subscribe(_session("left"))
    right = broker.subscribe(_session("right"))
    event = intents.publish_agent_event("agent_reply", agent_id="go2", status="ok",
        session_id="left", run_id="run-1", trace_id="tr-1", text="汪。")
    assert left.get_nowait()["event_id"] == event["event_id"]
    with pytest.raises(queue.Empty):
        right.get_nowait()
    intents.publish_agent_event("agent_reply", agent_id="go2", status="ok",
        session_id="left", run_id="run-1", trace_id="tr-1", text="汪。")
    with pytest.raises(queue.Empty):
        left.get_nowait()
    broker.unsubscribe("left", left)
    assert broker.subscribe(_session("left")).get_nowait()["event_id"] == event["event_id"]
    with pytest.raises(queue.Empty):
        broker.subscribe(_session("left"), event["event_id"]).get_nowait()


def test_space_reply_only_reaches_active_members(monkeypatch):
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, "_events", broker)
    member = broker.subscribe(_session("member"))
    other = broker.subscribe(_session("other", "elsewhere"))
    intents.publish_agent_event("agent_reply", agent_id="go2", status="ok",
        scope="space", space_id="main", source="autonomous", run_id="auto-1", text="汪。")
    assert member.get_nowait()["source"] == "autonomous"
    with pytest.raises(queue.Empty):
        other.get_nowait()


def test_feedback_bridge_binds_session_and_orchestrator_identity(monkeypatch):
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, "_events", broker)
    owner = broker.subscribe(_session("owner"))
    stranger = broker.subscribe(_session("stranger"))
    payload = {"type": "agent_reply", "agent_id": "cloud_orchestrator",
               "session_id": "stranger", "scope": "space", "space_id": "elsewhere",
               "run_id": "r", "trace_id": "tr", "status": "ok", "text": "完成"}
    assert intents.handle_mqtt_message("ssm/feedback/owner", json.dumps(payload))
    event = owner.get_nowait()
    assert event["session_id"] == "owner"
    assert event["scope"] == "session"
    with pytest.raises(queue.Empty):
        stranger.get_nowait()
    payload["agent_id"] = "go2"
    assert intents.handle_mqtt_message("ssm/feedback/owner", json.dumps(payload))
    with pytest.raises(queue.Empty):
        owner.get_nowait()


def test_orchestrator_progress_keeps_action_and_result_facts(monkeypatch):
    broker = intents._EventBroker()
    monkeypatch.setattr(intents, "_events", broker)
    owner = broker.subscribe(_session("owner"))
    step = {"tool": "invoke_skill", "params": {"unit_id": "esp32_desk_led",
            "skill_id": "set_light_state", "params": {"state": "ON"}},
            "result": {"result": "ok", "ism_state": "ON"}}
    facts = step_progress_facts(step)
    payload = {"type": "agent_progress", "agent_id": "cloud_orchestrator",
               "run_id": "run", "trace_id": "trace", "step_id": "step",
               "stage": "observed", "status": "ok", **facts}
    assert intents.handle_mqtt_message("ssm/feedback/owner", json.dumps(payload))
    event = owner.get_nowait()
    assert event["target_unit_id"] == "esp32_desk_led"
    assert event["skill_id"] == "set_light_state"
    assert event["arguments"] == {"state": "ON"}
    assert event["result_state"] == "ON"


def test_progress_stage_is_subtype_not_sse_event_name():
    assert intents._sse_event_name({"type": "agent_progress", "stage": "decision"}) == "agent_progress"
    assert intents._sse_event_name({"type": "agent_progress", "stage": "observed"}) == "agent_progress"
    assert intents._sse_event_name({"type": "agent_reply", "stage": "finished"}) == "agent_reply"
    assert intents._sse_event_name({"stage": "planning"}) == "planning"


def test_progress_facts_keep_delegated_goal_without_tool_output():
    step = {"tool": "invoke_skill", "params": {"unit_id": "go2", "skill_id": "go2_chat",
            "params": {"message": "坐下"}}, "result": {"result": "blocked", "tool_results": [
                {"tool": "go2_sport", "result": {"result": "blocked", "message": "Go2 未连接"}}]}}
    facts = step_progress_facts(step)
    assert facts["requested_text"] == "坐下"
    assert facts["reason"] == "Go2 未连接"


def test_plain_chat_reply_uses_answer_draft_without_execution_report():
    run = Run("chat", "s", "陪我聊两句", time.time() + 10,
              status="ok", decision_draft="好呀，我在这儿。")
    prompt = persona_reply_prompt("你是好奇的 Go2，讲话轻快。", run,
                                  traits={"curiosity": 80})
    assert '"answer_draft": "好呀，我在这儿。"' in prompt
    assert '"user_message": "陪我聊两句"' in prompt
    assert "好奇的 Go2" in prompt
    assert "不要提任务完成、执行记录、状态、没有动作步骤" in prompt
    assert '"runtime_note"' not in prompt
    assert '"status"' not in prompt


def test_go2_extraversion_changes_reply_guidance():
    profile = {"prompt": "你是 Go2。", "traits": {"extraversion": 20}}
    assert "不主动延长闲聊" in reply_persona(profile)
    profile["traits"]["extraversion"] = 85
    assert "可自然接话" in reply_persona(profile)


def test_narrated_progress_uses_facts_and_drops_late_text(monkeypatch):
    monkeypatch.setattr("cloud.agents.common.progress._slots", threading.BoundedSemaphore(2))
    received = []
    arrived = threading.Event()
    closed = threading.Event()
    step = {"step_id": "step-1", "tool": "move", "params": {},
            "result": {"result": "accepted"}}

    def invoke(prompt):
        assert '"result": "accepted"' in prompt
        return "已经受理，仍在等动作确认。"

    submit_progress("狗的人设", "移动", step, invoke,
                    lambda text: (received.append(text), arrived.set()),
                    closed, time.time() + 2)
    assert arrived.wait(1)
    assert received == ["已经受理，仍在等动作确认。"]

    started = threading.Event()
    release = threading.Event()
    def slow_invoke(_prompt):
        started.set()
        release.wait(1)
        return "迟到的进展"
    submit_progress("狗的人设", "移动", step, slow_invoke,
                    received.append, closed, time.time() + 2)
    assert started.wait(1)
    closed.set()
    release.set()
    time.sleep(0.05)
    assert received == ["已经受理，仍在等动作确认。"]
