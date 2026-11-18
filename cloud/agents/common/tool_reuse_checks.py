"""Once-per-run effects across model turns, batches and persisted Graph state."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, ConfigDict

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.runner import GraphRun, run_role_graph


class Operation(BaseModel):
    """Perform an operation on a named resource."""

    model_config = ConfigDict(extra="forbid")
    resource: str = "main"


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.inputs = []
        self.tools = []

    def bind_tools(self, tools, **_kwargs):
        self.tools.append([tool["function"]["name"] for tool in tools])
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages)
        return next(self.responses)


def call(tool="reserve", call_id="first", **args):
    return {"name": tool, "id": call_id, "args": args}


def decision(*calls):
    return AIMessage(content="", tool_calls=list(calls))


@pytest.fixture
def options(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_USER_GOALS_DIR", str(tmp_path / "goals"))
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", Mock())
    return {
        "tool_schemas": {name: Operation for name in ("reserve", "notify", "move", "read")},
        "once_per_run_tools": frozenset({"reserve"}),
        "effectful": lambda name: name != "read",
        "build_messages": lambda run: [HumanMessage(content=run.goal)],
        "checkpointer": FileCheckpointSaver(tmp_path / "checkpoints"),
        "effect_ledger": FileEffectLedger(tmp_path / "effects"),
    }


def make_run(agent_id="cloud_orchestrator", **kwargs):
    return GraphRun("reuse", "session", "完成当前目标", time.time() + 30,
                    agent_id=agent_id, **kwargs)


@pytest.mark.parametrize("agent_id", ["cloud_orchestrator", "esp32_desk_led", "go2"])
@pytest.mark.parametrize("status", ["ok", "accepted"])
async def test_new_call_id_reuses_success_and_closes_without_another_step(agent_id, status, options):
    run = make_run(agent_id)
    model = Model(decision(call(final=False)),
                  decision(call(call_id="repeat", resource="main", final=False)))
    execute = Mock(return_value={"result": status, "task_id": "original-task"})
    progress = []

    await run_role_graph(run, model=model, execute=execute, max_effects=1,
        on_progress=lambda phase, step: progress.append((phase, deepcopy(step))), **options)

    execute.assert_called_once_with("reserve", {"resource": "main"}, "first")
    assert run.status == "ok" and len(run.steps) == 1
    assert run.steps[0]["result"]["task_id"] == "original-task"
    assert run.steps[0]["result"]["final"] is False  # Original receipt stays intact.
    assert len(model.inputs) == 2
    assert "reserve" not in model.tools[-1] and "notify" in model.tools[-1]
    assert sum(phase == "decision" for phase, _ in progress) == 1
    receipts = [json.loads(message.content) for message in model.inputs[1]
                if isinstance(message, ToolMessage)]
    assert receipts[0]["task_id"] == "original-task"


async def test_repeated_operation_does_not_drop_a_queued_new_operation(options):
    run = make_run()
    model = Model(decision(call(final=False)), decision(
        call(call_id="repeat", final=False), call("notify", "next")))
    execute = Mock(return_value={"result": "ok"})

    await run_role_graph(run, model=model, execute=execute, **options)

    assert [args.args[0] for args in execute.call_args_list] == ["reserve", "notify"]
    assert [step["step_id"] for step in run.steps] == ["first", "next"]
    assert run.status == "ok"


async def test_duplicate_calls_in_one_batch_execute_once(options):
    run = make_run()
    model = Model(decision(call(), call(call_id="duplicate")))
    execute = Mock(return_value={"result": "ok"})

    await run_role_graph(run, model=model, execute=execute, **options)

    assert execute.call_count == len(run.steps) == len(model.inputs) == 1
    assert run.status == "ok"


async def test_final_repeat_discards_queued_calls_and_uses_tool_free_closing(options):
    run = make_run()
    planner = Model(decision(call(final=False)), decision(
        call(call_id="repeat", final=True), call("notify", "unwanted")))
    from cloud.agents.common.replies import finalize_reply
    speaker = Model(AIMessage(content="已安排完成。"))
    execute = Mock(return_value={"result": "ok"})

    await run_role_graph(run, model=planner, execute=execute,
        finalize=lambda current: finalize_reply(current, model=speaker, instructions="根据真实回执回复"),
        **options)

    assert execute.call_count == 1 and run.status == "ok"
    assert run.response == "已安排完成。" and speaker.tools == []
    assert len(speaker.inputs) == 1


@pytest.mark.parametrize("tool", ["move", "read"])
async def test_repeatable_effects_and_fresh_queries_still_execute_each_time(tool, options):
    run = make_run()
    model = Model(decision(call(tool, final=False)),
                  decision(call(tool, "second", final=True)))
    execute = Mock(side_effect=[{"result": "ok", "value": 1}, {"result": "ok", "value": 2}])

    await run_role_graph(run, model=model, execute=execute, **options)

    assert execute.call_count == 2 and run.status == "ok"
    assert [step["result"]["value"] for step in run.steps] == [1, 2]
    assert "reserve" in model.tools[-1]


async def test_success_cannot_be_reused_for_different_arguments(options):
    run = make_run()
    model = Model(decision(call(final=False)),
                  decision(call(call_id="changed", resource="other")))
    execute = Mock(return_value={"result": "ok", "resource": "main"})

    await run_role_graph(run, model=model, execute=execute, **options)

    assert execute.call_count == 1 and run.status == "rejected"
    assert run.steps[0]["result"]["resource"] == "main"


@pytest.mark.parametrize("status", ["blocked", "rejected", "error"])
async def test_failed_attempt_remains_retryable_and_is_not_mistaken_for_success(status, options):
    run = make_run()
    model = Model(decision(call(final=False)), decision(call(call_id="retry")))
    execute = Mock(side_effect=[{"result": status}, {"result": "ok"}])

    await run_role_graph(run, model=model, execute=execute, **options)

    assert execute.call_count == 2 and "reserve" in model.tools[-1]
    assert run.status == "partial"  # This policy does not erase genuine failures.


@pytest.mark.parametrize("status", [None, "executing", "unknown", "timeout"])
@pytest.mark.parametrize("call_id,resource", [("new-id", "other"), ("original", "main")])
async def test_saved_unconfirmed_effect_is_not_redispatched(status, call_id, resource, options):
    step = {"tool": "reserve", "step_id": "original", "params": {"resource": "main"},
            "effectful": True, "dispatch_status": "pending" if status is None else "observed",
            "result": None if status is None else {"result": status, "task_id": "unconfirmed"}}
    run = make_run(steps=[deepcopy(step)])
    model = Model(decision(call(call_id=call_id, resource=resource)))

    await run_role_graph(run, model=model,
        execute=lambda *_: pytest.fail("unconfirmed effect was dispatched again"), **options)

    assert run.status == (status or "unknown") and run.steps == [step]
    assert "reserve" not in model.tools[-1]


async def test_original_call_still_reconciles_its_saved_ledger_receipt(options):
    run = make_run(steps=[{"tool": "reserve", "step_id": "original",
        "params": {"resource": "main"}, "effectful": True,
        "dispatch_status": "pending", "result": None}])
    options["effect_ledger"].reserve(run.agent_id, thread_id_for(run.agent_id, run.session_id),
        run.run_id, "original", "reserve", {"resource": "main"})
    reconcile = Mock(return_value={"result": "ok", "task_id": "physical-original"})

    await run_role_graph(run, model=Model(decision(call(call_id="original"))),
        execute=lambda *_: pytest.fail("original effect was dispatched again"),
        reconcile_effect=reconcile, **options)

    reconcile.assert_called_once_with("reserve", {"resource": "main"}, "original")
    assert run.status == "ok" and len(run.steps) == 1
    assert run.steps[-1]["result"]["task_id"] == "physical-original"


async def test_checkpoint_resume_keeps_completed_once_effect_and_allows_new_work(options):
    run = make_run()
    first = Model(decision(call(final=False)),
                  AIMessage(content='{"kind":"question","text":"通知哪个资源？"}'))
    execute = Mock(return_value={"result": "ok", "task_id": "original-task"})
    await run_role_graph(run, model=first, execute=execute, **options)
    assert run.status == "needs_clarification"

    resumed = make_run()
    model = Model(decision(call(call_id="repeat"), call("notify", "next", resource="other")))
    reopened = {**options, "checkpointer": FileCheckpointSaver(options["checkpointer"].root),
                "effect_ledger": FileEffectLedger(options["effect_ledger"].root)}
    await run_role_graph(resumed, model=model, execute=execute,
                         resume=True, resume_value="other", **reopened)

    assert [args.args[0] for args in execute.call_args_list] == ["reserve", "notify"]
    assert resumed.status == "ok" and len(resumed.steps) == 2
    assert "reserve" not in model.tools[-1]


async def test_completed_once_effect_does_not_leak_into_the_next_user_goal(options):
    execute = Mock(return_value={"result": "ok"})
    for run_id in ("first-goal", "next-goal"):
        run = make_run()
        run.run_id = run_id
        await run_role_graph(run, model=Model(decision(call())), execute=execute, **options)
        assert run.status == "ok"
    assert execute.call_count == 2
