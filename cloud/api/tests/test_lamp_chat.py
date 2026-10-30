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
"""Unified personality persistence and administrator routes."""

import pytest

from cloud.agent_runtime.personality import ProfileStore, RevisionConflict
from cloud.api.personality import router as personality_router
from cloud.api.tests.conftest import login_admin
from cloud.go2.agentcore.personality import profile as go2_profile


def _store(tmp_path, migration=None):
    return ProfileStore(agent_id="go2", path=tmp_path / "personality" / "profile.json",
                        prompt="默认", trait_specs=go2_profile.TRAIT_SPECS,
                        migration=migration)


def test_profile_migration_revision_validation_and_evolution(tmp_path):
    old = {"prompt": "管理员原有文案", "traits": {"curiosity": 82, "extraversion": 30,
                                            "boldness": 44},
           "metadata": {"last_evolved": "2026-10-28"}}
    store = _store(tmp_path, lambda: old)
    first = store.get_profile()
    assert first["prompt"] == "管理员原有文案"
    assert first["traits"]["curiosity"] == 82
    assert store.metadata()["last_evolved"] == "2026-10-28"
    old["prompt"] = "不应覆盖迁移结果"
    assert store.get_profile() == first

    admin = {"actor": "admin", "persist": True}
    edited = store.update_profile({"revision": first["revision"],
                                   "traits": {"curiosity": 40}}, admin)
    with pytest.raises(RevisionConflict):
        store.update_profile({"revision": first["revision"], "prompt": "过期"}, admin)
    with pytest.raises(ValueError):
        store.update_profile({"revision": edited["revision"], "traits": {"unknown": 20}}, admin)
    with pytest.raises(ValueError):
        store.update_profile({"revision": edited["revision"], "traits": {"boldness": 101}}, admin)
    with pytest.raises(PermissionError):
        store.update_profile({"revision": edited["revision"], "prompt": "访客"},
                             {"actor": "visitor", "persist": True})

    current = store.evolve({"curiosity": 2}, "2026-10-29")
    assert current["traits"]["curiosity"] == 42
    assert store.metadata()["last_evolved"] == "2026-10-29"
    assert not store.replace_generated_prompt("旧生成结果", expected_revision=edited["revision"])
    assert store.get_profile()["prompt"] == "管理员原有文案"


@pytest.mark.asyncio
async def test_personality_api_admin_csrf_and_revisions(app, client, monkeypatch, tmp_path):
    from cloud.orchestrator.agentcore.personality import profile as coordinator_profile
    store = ProfileStore(agent_id="cloud_orchestrator",
                         path=tmp_path / "orchestrator" / "personality" / "profile.json",
                         prompt="管家", trait_specs=coordinator_profile.TRAIT_SPECS)
    monkeypatch.setattr(coordinator_profile, "_store", store)
    app.include_router(personality_router)
    assert (await client.get("/api/agents/cloud_orchestrator/personality")).status_code == 401
    headers = await login_admin(client)
    first = await client.get("/api/agents/cloud_orchestrator/personality")
    assert first.status_code == 200
    revision = first.json()["revision"]
    body = {"revision": revision, "traits": {"warmth": 85}}
    assert (await client.put("/api/agents/cloud_orchestrator/personality", json=body)).status_code == 403
    updated = await client.put("/api/agents/cloud_orchestrator/personality",
                               json=body, headers=headers)
    assert updated.status_code == 200
    assert updated.json()["traits"]["warmth"] == 85
    assert (await client.put("/api/agents/cloud_orchestrator/personality",
                             json=body, headers=headers)).status_code == 409
    assert (await client.put("/api/agents/other/personality",
                             json=body, headers=headers)).status_code == 404
