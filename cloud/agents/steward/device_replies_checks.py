"""Narrow reply aggregation regressions; no services, models or runtime files."""

import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from cloud.agents.common.runner import GraphRun
from cloud.agents.steward.device_replies import device_outcomes, publish_device_replies


def delegated(step_id, unit_id, skill_id, result):
    return {"step_id": step_id, "tool": "invoke_skill",
            "params": {"unit_id": unit_id, "skill_id": skill_id, "params": {}},
            "result": {"unit_id": unit_id, "skill_id": skill_id, **result}}


@pytest.mark.asyncio
async def test_each_device_speaks_once_after_all_receipts_and_uses_original_goal(monkeypatch):
    monkeypatch.setenv("ESP32_LED_UNIT_ID", "test_lamp")
    monkeypatch.setattr("cloud.agents.lamp.prompts.persona_reply_prompt", lambda *_: "lamp")
    monkeypatch.setattr("cloud.agents.go2.prompts.persona_reply_prompt", lambda *_: "go2")
    monkeypatch.setattr("cloud.agents.steward.device_replies.emit_agent_event", lambda *a, **kw: None)
    run = GraphRun("parent", "session", "狗跳舞，灯亮紫色", time.time() + 60, steps=[
        delegated("dance", "go2", "go2_sport", {"result": "timeout", "tool_results": [
            {"tool": "go2_sport", "params": {"cmd": "Dance1"}, "result": {
                "result": "timeout", "motion_observed": True}}]}),
        delegated("color", "test_lamp", "set_light_color", {"result": "ok"}),
        delegated("on", "test_lamp", "set_light_state", {"result": "ok", "physical_state": {
            "ism": "ON", "r": 255, "g": 255, "b": 255, "ts": 123}}),
        delegated("status", "go2", "go2_status", {"result": "ok", "tool_results": [
            {"tool": "go2_status", "effectful": False, "result": {"result": "ok"}}]}),
    ], status="partial", trace_id="trace")
    outputs, inputs = [], []

    class Model:
        async def ainvoke(self, messages):
            inputs.append(messages)
            return AIMessage(content="最终设备结果。")

    await publish_device_replies(run, context={"expires_at": time.time() + 60},
        transport=SimpleNamespace(do_publish_ui_event=lambda *a, **kw: outputs.append(kw)),
        model_factory=lambda role: Model())
    assert [item["agent_id"] for item in outputs] == ["go2", "test_lamp"]
    assert outputs[0]["status"] == "timeout", "A successful status query cannot complete the dance"
    assert len(inputs) == 2
    assert all(any(message.content == run.goal for message in messages) for messages in inputs)
    lamp = run.initial_context["delegated_device_outcomes"]["test_lamp"]
    assert len(lamp["steps"]) == 2
    assert lamp["physical_state"]["ts"] == 123
    assert lamp["physical_state"]["r"] == 255


@pytest.mark.asyncio
async def test_internal_status_query_is_silent(monkeypatch):
    run = GraphRun("parent", "session", "查询状态", time.time() + 60, steps=[
        delegated("status", "go2", "go2_status", {"result": "ok"})])
    published = []
    await publish_device_replies(run, context={},
        transport=SimpleNamespace(do_publish_ui_event=lambda *a, **kw: published.append(kw)),
        model_factory=lambda _: pytest.fail("A status query must not generate device speech"))
    assert published == []
    assert device_outcomes(run)["go2"]["status"] == "ok"
