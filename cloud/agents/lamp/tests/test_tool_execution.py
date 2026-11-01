import time
from types import SimpleNamespace

from cloud.agents.lamp.tools import execute as lamp_execute
from cloud.esp32.physical_tasks import stable_task_id
from cloud.space.execution import ExecutionContext


def test_graph_step_uses_stable_physical_task_id(monkeypatch):
    calls = []
    monkeypatch.setattr(lamp_execute, "execute_physical",
                        lambda **kwargs: calls.append(kwargs) or {"result": "ok"})
    context = ExecutionContext("session_1", "admin", True, "main",
                               "user_delegated", int(time.time()) + 100)
    agent = SimpleNamespace(_state=object())
    result = lamp_execute.execute_lamp_tool(
        "set_led_state", {"state": "ON"}, "call_1", agent=agent,
        unit_id="esp32_desk_led", context=context, dispatch=lambda *_args: None,
        lighting_only=False, run_id="run_1",
    )
    assert result["result"] == "ok"
    assert calls[0]["task_id"] == stable_task_id("esp32_desk_led", "run_1", "call_1")
