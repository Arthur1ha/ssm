"""Regression checks for the lamp speaking as itself after failed actions."""

from types import SimpleNamespace

from cloud.agents.common.replies import factual_fallback
from cloud.agents.lamp.prompts import valid_lamp_reply


def test_lamp_rejects_detached_voice_and_unobserved_receipt():
    run = SimpleNamespace(goal="闪烁三次提醒我", status="error", steps=[])
    assert not valid_lamp_reply("灯那边没回话，等我拿到回执再说。", run)
    assert not valid_lamp_reply("我还没和灯建立联系。", run)
    assert valid_lamp_reply("我这回没接住你的话，也没动灯。", run)


def test_lamp_no_dispatch_fallback_uses_first_person():
    run = SimpleNamespace(goal="闪烁三次提醒我", status="error", steps=[])
    reply = factual_fallback("esp32_desk_led", "error", run)
    assert "我" in reply and "还没做任何动作" in reply
    assert "灯那边" not in reply and "回执" not in reply
