"""Regression checks for the lamp speaking as itself after failed actions."""

from types import SimpleNamespace

from cloud.agents.common.replies import factual_fallback
from cloud.agents.common.replies import valid_reply_format
from cloud.agents.lamp.prompts import persona_reply_prompt


def test_lamp_voice_is_model_guidance_not_a_wording_gate():
    profile = {"agent_id": "lamp", "name": "灯", "prompt": "自然说话", "traits": {}}
    prompt = persona_reply_prompt(None, profile)
    assert "第一人称" in prompt
    assert "除非用户询问数值" in prompt
    assert valid_reply_format("我还没和灯建立联系。")
    assert valid_reply_format("亮度为120，RGB 255,255,255。")


def test_lamp_no_dispatch_fallback_uses_first_person():
    run = SimpleNamespace(goal="闪烁三次提醒我", status="error", steps=[])
    reply = factual_fallback("esp32_desk_led", "error", run)
    assert "我" in reply and "还没做任何动作" in reply
    assert "灯那边" not in reply and "回执" not in reply
