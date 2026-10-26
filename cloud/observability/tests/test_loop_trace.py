import json
import time

import pytest

from cloud.agent_runtime.loop import Run, run_loop
from cloud.observability import configure_logging


def _events(directory):
    path = next(directory.glob("agent-trace-*.jsonl"))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_trace_records_dispatch_after_durable_pending_intent(tmp_path):
    configure_logging("api", log_dir=tmp_path)
    run = Run("run_trace", "session", "turn on the light", time.time() + 10,
              trace_id="tr_0123456789abcdef0123456789abcdef", agent_id="lamp")
    records = []

    async def decide(current):
        return ({"tool": "set_led_state", "params": {"state": "ON"}}
                if not current.steps else {"final": "灯亮了"})

    async def execute(name, params, step_id):
        assert records[-1] == (step_id, "pending")
        return {"result": "ok", "task_id": "task_one"}

    def record(current, step):
        if step:
            records.append((step["step_id"], step["dispatch_status"]))

    await run_loop(run, decide, execute, record=record)
    events = _events(tmp_path)
    names = [event["event"] for event in events]
    assert names.index("tool.dispatch") < names.index("tool.result") < names.index("run.finished")
    assert all(event["trace_id"] == run.trace_id for event in events)
    assert records[0][1] == "pending"
    assert run.status == "ok"


@pytest.mark.asyncio
async def test_model_failure_never_dispatches_tool(tmp_path):
    configure_logging("api", log_dir=tmp_path)
    run = Run("run_timeout", "session", "move", time.time() + 10,
              trace_id="tr_fedcba9876543210fedcba9876543210", agent_id="go2")

    async def decide(_run):
        raise TimeoutError("upstream timeout")

    async def execute(_name, _params, _step_id):
        raise AssertionError("tool must not run")

    await run_loop(run, decide, execute)
    names = [event["event"] for event in _events(tmp_path)]
    assert "model.error" in names
    assert "tool.dispatch" not in names
    assert run.status == "error"
