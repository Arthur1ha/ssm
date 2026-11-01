"""A physical MQTT command is published at most once per stable task ID."""

import asyncio

from cloud.api import intents, main
from cloud.esp32 import physical_tasks


def test_duplicate_dispatch_and_api_restart_query_original_id(monkeypatch, tmp_path):
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path)
    monkeypatch.setattr(intents, "ensure_instant_action", lambda _action: None)
    monkeypatch.setattr(intents, "_configured_card", lambda _unit: {
        "online": True, "transport": {"kind": "mqtt"},
        "skills": [{"invoke": {"action": "SET_STATE"}}],
        "state_machine": {"states": ["OFF", "ON"],
                          "transitions": [{"src": "OFF", "action": "SET_STATE",
                                           "params": {"state": "ON"}}]},
    })
    from cloud.esp32 import tool_registry as registry
    monkeypatch.setattr(registry, "fsm_allows", lambda *_args: True)
    monkeypatch.setattr(main._esp32_state, "actuator_snapshot",
                        lambda: {"esp32_desk_led": {"state": {"ism": "OFF"}}})
    published = []
    monkeypatch.setattr(intents, "_mqtt_publish", lambda topic, payload: published.append((topic, payload)))
    session = {"session_id": "session_1", "actor": "admin", "persist": False}
    task_id = physical_tasks.stable_task_id("esp32_desk_led", "run_1", "step_1")

    for _ in range(2):
        assert intents._dispatch_mqtt_task("esp32_desk_led", "SET_STATE", {"state": "ON"},
                                           session, task_id=task_id) == task_id
    assert len(published) == 1
    with intents._lamp_task_owners_lock:
        intents._lamp_task_owners.clear()  # API process restart
        intents._lamp_task_results.clear()
    unknown = asyncio.run(intents.get_lamp_command_task("esp32_desk_led", task_id, session))
    assert unknown["result"] == "unknown"

    intents.record_lamp_task_result(task_id, {"result": "ok", "task_id": task_id})
    done = asyncio.run(intents.get_lamp_command_task("esp32_desk_led", task_id, session))
    assert done["result"] == "ok"
    assert len(published) == 1
