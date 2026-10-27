"""Device chat boundaries that prevent a long wait or partial physical action."""

import time
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cloud.api import intents
from cloud.esp32 import agent as esp32_agent


@pytest.mark.asyncio
async def test_lamp_run_waits_for_final_persona_reply(monkeypatch, tmp_path):
    from cloud.agent_runtime.loop import Run
    from cloud.agent_runtime.store import RunStore
    from cloud.esp32 import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    store = RunStore(tmp_path / "runs")
    run = Run("lamp_reply_race", "test-session", "亮紫灯，然后让狗坐下", time.time() + 90)
    run.status = "error"
    run.response = "暂时无法可靠理解下一步；已执行的动作不会重发。"
    run.reply_ready = False
    assert store.create(run)
    session = {"session_id": "test-session"}

    pending = await intents.get_lamp_chat_run("esp32_desk_led", run.run_id, session)
    assert pending["result"] == "executing"
    assert pending["reply"] == ""

    run.response = "紫灯已经亮了；狗坐下的请求被拒了。"
    run.reply_source = "persona_model"
    run.reply_ready = True
    store.save(run)
    final = await intents.get_lamp_chat_run("esp32_desk_led", run.run_id, session)
    assert final["result"] == "error"
    assert final["reply"] == run.response


def test_sequential_device_request_reaches_result_driven_loop(monkeypatch):
    call = MagicMock(return_value={"result": "needs_clarification", "reply": "移动几秒？",
                                   "run_id": "run-1"})
    lamp = SimpleNamespace(run_user_loop=call)
    session = {"session_id": "test-session", "actor": "admin", "expires_at": 9999999999}

    result = intents._run_lamp_chat(
        lamp,
        "esp32_desk_led", "把灯关掉，然后让机器狗后退几步", session,
    )

    assert result["result"] == "needs_clarification"
    assert result["run_id"] == "run-1"
    call.assert_called_once()


def test_lamp_llm_does_not_retry_past_chat_deadline(monkeypatch):
    captured = {}

    def make_model(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("MODEL_LIST", "test-model")
    monkeypatch.setattr(esp32_agent, "ChatOpenAI", make_model)
    esp32_agent.ESP32Agent.__new__(esp32_agent.ESP32Agent)._make_llm()

    assert captured["timeout"] == 35
    assert captured["max_retries"] == 0


def test_lamp_command_checks_current_fsm_before_publish(monkeypatch):
    from cloud.api import main as api_main

    card = {
        "online": True, "transport": {"kind": "mqtt"},
        "skills": [{"invoke": {"action": "SET_COLOR"}}],
        "state_machine": {
            "states": ["ON", "ERROR"],
            "transitions": [{"src": "ON", "dst": "ON", "action": "SET_COLOR"}],
        },
    }
    monkeypatch.setattr(intents, "_configured_card", lambda _unit_id: card)
    monkeypatch.setattr(api_main, "_esp32_state", MagicMock(
        actuator_snapshot=lambda: {"esp32_desk_led": {"state": {"ism": "ERROR"}}},
    ))
    publish = MagicMock()
    monkeypatch.setattr(intents, "_mqtt_publish", publish)

    with pytest.raises(HTTPException) as exc:
        intents._dispatch_mqtt_task(
            "esp32_desk_led", "SET_COLOR",
            {"r": 1, "g": 2, "b": 3, "brightness": 4},
            {"session_id": "session", "actor": "admin"},
        )
    assert exc.value.status_code == 409
    publish.assert_not_called()
