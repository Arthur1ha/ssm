"""Semantic lamp delegation confirms only the ESP32 result and is idempotent."""

import asyncio
import inspect
import time

from cloud.esp32 import agent as agent_mod
from cloud.esp32 import router as lamp_router
from cloud.esp32.agentcore.tools import registry as tools_registry
from cloud.esp32.state import ESP32State
from cloud.space.execution import ExecutionContext


def test_semantic_task_waits_for_physical_result_and_reuses_id(monkeypatch, tmp_path):
    from cloud.esp32 import tasks
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "semantic_tasks.sqlite3")
    tasks._tasks.clear()
    state = ESP32State()
    agent = agent_mod.ESP32Agent(state, llm=object())
    agent.handle_user_text = lambda text, **kwargs: {
        "type": "act", "action": "SET_STATE", "params": {"state": "ON"},
        "reply": "开好了。",
    }
    monkeypatch.setattr(agent_mod, "get_agent", lambda: agent)
    monkeypatch.setattr(lamp_router._esp32_agent, "get_agent", lambda: agent)
    from cloud.cards import registry as card_registry
    class Cards:
        def get_card(self, unit_id):
            return {"online": True, "skills": [{"id": "adjust_lighting"}]}
    monkeypatch.setattr(card_registry, "get_registry", lambda: Cards())

    published = []
    from cloud.api import intents
    def dispatch(unit_id, action, params, session, source=None):
        published.append((unit_id, action, params, session, source))
        return "physical_semantic_1"
    monkeypatch.setattr(intents, "_dispatch_mqtt_task", dispatch)
    monkeypatch.setitem(tools_registry.PHYSICAL_ACTIONS["SET_STATE"], "timeout", 0.02)
    context = ExecutionContext("semantic_test_session", "admin", True,
                               "main", "user_delegated", int(time.time()) + 100)
    request = lamp_router.SemanticTaskRequest(goal="柔和一点", task_id="semantic_test_1")

    first = lamp_router.invoke_lamp_semantic_task("esp32_desk_led", request, context)
    assert first["result"] == "timeout"
    assert first["physical_task_id"] == "physical_semantic_1"
    assert published[0][3]["persist"] is False
    assert len(published) == 1

    state.store_task_result("physical_semantic_1", {"result": "ok"})
    later = asyncio.run(lamp_router.get_lamp_semantic_task(
        "esp32_desk_led", "semantic_test_1", context,
    ))
    assert later["result"] == "ok"
    repeated = lamp_router.invoke_lamp_semantic_task("esp32_desk_led", request, context)
    assert repeated["result"] == "ok"
    assert len(published) == 1
    tasks._tasks.clear()  # Simulate memory-cache eviction or process restart.
    after_restart = lamp_router.invoke_lamp_semantic_task("esp32_desk_led", request, context)
    assert after_restart["result"] == "ok"
    assert len(published) == 1


def test_semantic_task_wait_uses_fastapi_worker_pool():
    # FastAPI offloads ordinary def handlers. An async handler would block its
    # event loop while run_once waits for physical completion.
    assert not inspect.iscoroutinefunction(lamp_router.invoke_lamp_semantic_task)
