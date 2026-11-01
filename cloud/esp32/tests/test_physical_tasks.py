import pytest
import time

from cloud.esp32 import physical_tasks
from cloud.esp32.tool_executor import execute_physical
from cloud.space.execution import ExecutionContext


def test_reservation_survives_reload_and_never_republishes(monkeypatch, tmp_path):
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path)
    task_id = physical_tasks.stable_task_id("esp32_desk_led", "run_1", "step_1")
    assert task_id == physical_tasks.stable_task_id("esp32_desk_led", "run_1", "step_1")
    kwargs = dict(unit_id="esp32_desk_led", session_id="session_1",
                  action="SET_STATE", params={"state": "ON"})
    assert physical_tasks.reserve(task_id, **kwargs)
    assert not physical_tasks.reserve(task_id, **kwargs)
    assert physical_tasks.get(task_id, unit_id="esp32_desk_led",
                              session_id="session_1")["result"] == "unknown"
    with pytest.raises(ValueError, match="physical_task_id_conflict"):
        physical_tasks.reserve(task_id, **{**kwargs, "params": {"state": "OFF"}})
    assert physical_tasks.record_result(task_id, "esp32_desk_led",
                                        {"result": "ok", "task_id": task_id})
    assert physical_tasks.get(task_id, unit_id="esp32_desk_led",
                              session_id="session_1")["result"] == "ok"
    assert physical_tasks.get(task_id, unit_id="esp32_desk_led",
                              session_id="another") is None


def test_recovery_queries_reserved_id_without_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path)
    task_id = physical_tasks.stable_task_id("esp32_desk_led", "run_2", "step_1")
    physical_tasks.reserve(task_id, unit_id="esp32_desk_led", session_id="session_2",
                           action="SET_STATE", params={"state": "ON"})
    context = ExecutionContext("session_2", "admin", True, "main",
                               "user_delegated", int(time.time()) + 100)
    def dispatch(*_args, **_kwargs):
        raise AssertionError("must not republish")
    result = execute_physical(state=None, unit_id="esp32_desk_led", action="SET_STATE",
                              params={"state": "ON"}, context=context, dispatch=dispatch,
                              task_id=task_id)
    assert result["result"] == "unknown"
    physical_tasks.record_result(task_id, "esp32_desk_led",
                                 {"result": "ok", "task_id": task_id})
    result = execute_physical(state=None, unit_id="esp32_desk_led", action="SET_STATE",
                              params={"state": "ON"}, context=context, dispatch=dispatch,
                              task_id=task_id)
    assert result["result"] == "ok"
