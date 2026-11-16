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
async def test_each_device_speaks_once_after_all_receipts_and_uses_original_goal(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
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


@pytest.mark.parametrize('fail', [False, True])
@pytest.mark.asyncio
async def test_delegated_reply_generation_and_publication_are_not_replayed(tmp_path, monkeypatch, fail):
    from copy import deepcopy
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    monkeypatch.setenv('ESP32_LED_UNIT_ID', 'fixture_lamp')
    monkeypatch.setattr('cloud.agents.lamp.prompts.persona_reply_prompt', lambda *_: '灯人格')
    monkeypatch.setattr('cloud.agents.steward.device_replies.emit_agent_event', lambda *_a, **_k: None)
    calls, outputs = [], []
    run = GraphRun('reply-parent', 'session', '开灯', time.time() + 60,
        steps=[delegated('step', 'fixture_lamp', 'set_light_state', {'result': 'ok'})], status='ok')

    class Model:
        async def ainvoke(self, messages):
            calls.append(messages)
            if fail:
                raise RuntimeError('model unavailable')
            return AIMessage(content='{"text":"亮起来了。","emotion":"relaxed"}')

    options = dict(context={}, model_factory=lambda _: Model(),
                   transport=SimpleNamespace(do_publish_ui_event=lambda *a, **fields: outputs.append((a, fields))))
    await publish_device_replies(run, **options)
    await publish_device_replies(deepcopy(run), **options)
    assert len(calls) == len(outputs) == 1
    fields = outputs[0][1]
    assert fields['event_id'].startswith('reply_')
    if fail:
        assert outputs[0][0][1] == 'reply_error'
        assert fields['reply_source'] == 'runtime'
        assert '角色回复暂时不可用' in fields['text']
    else:
        assert fields['emotion'] == 'relaxed' and fields['text'] == '亮起来了。'


@pytest.mark.asyncio
async def test_original_delegated_reply_preserves_emotion_and_skips_composition(tmp_path, monkeypatch):
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    monkeypatch.setenv('ESP32_LED_UNIT_ID', 'fixture_lamp')
    monkeypatch.setattr('cloud.agents.lamp.prompts.persona_reply_prompt', lambda *_: '灯人格')
    monkeypatch.setattr('cloud.agents.steward.device_replies.emit_agent_event', lambda *_a, **_k: None)
    outputs = []
    run = GraphRun('original-parent', 'session', '开灯', time.time() + 60,
        steps=[delegated('step', 'fixture_lamp', 'set_light_state', {
            'result': 'ok', 'reply': '这就亮了。', 'emotion': 'happy', 'reply_source': 'decision_model'})], status='ok')
    await publish_device_replies(run, context={},
        model_factory=lambda _: pytest.fail('The original final reply must be reused'),
        transport=SimpleNamespace(do_publish_ui_event=lambda *_a, **fields: outputs.append(fields)))
    assert outputs[0]['text'] == '这就亮了。' and outputs[0]['emotion'] == 'happy'
