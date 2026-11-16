"""Confirmed emotion expressions use the existing durable tool boundary."""

import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.replies import finalize_reply
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.common.training.models import ApplyTraining


REFERENCE = {"unit_id": "go2", "teaching_id": "happy", "revision": 1, "overrides": {}}


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0

    def bind_tools(self, *_args, **_kwargs):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        return next(self.responses)


def dialogue(kind="final", emotion="happy"):
    return AIMessage(content=json.dumps({"kind": kind, "text": "谢谢你夸我！", "emotion": emotion}))


@pytest.fixture
def options(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *_a, **_k: None)
    return {"tool_schemas": {"apply_training": ApplyTraining},
            "build_messages": lambda run: [HumanMessage(content=run.goal)],
            "checkpointer": FileCheckpointSaver(tmp_path / "checkpoints"),
            "effect_ledger": FileEffectLedger(tmp_path / "effects")}


def new_run():
    return GraphRun("expression", "session", "你真棒", time.time() + 30, agent_id="go2")


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [{"result": "ok"}, {"result": "accepted", "task_id": "device"}])
async def test_expression_uses_training_receipt_and_preserves_one_reply(options, receipt):
    run, model, dispatched = new_run(), Model(dialogue()), []

    def execute(name, arguments, step_id):
        dispatched.append((name, arguments, step_id))
        return receipt

    await run_role_graph(run, model=model, execute=execute,
        prepare_emotion=lambda current: dict(REFERENCE),
        finalize=lambda current: finalize_reply(current, model=model, instructions="回应用户。"), **options)
    assert dispatched == [("apply_training", REFERENCE, "expression:emotion")]
    assert run.status == "ok" and run.emotion == "happy"
    assert run.response == "谢谢你夸我！" and model.calls == 1
    assert run.steps[0]["result"]["result"] == "ok"
    assert run.steps[0]["result"]["final"] is True
    assert run.emotion_step_id == "expression:emotion"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "rejected", "error", "timeout", "unknown", "executing", "cancelled"])
async def test_failed_expression_discards_draft_and_never_replans(options, status):
    run = new_run()
    planner = Model(dialogue())
    speaker = Model(AIMessage(content='{"text":"这次动作尚未完成。","emotion":"sad"}'))
    dispatched = []

    def execute(*args):
        dispatched.append(args)
        return {"result": status, "error": "device_feedback"}

    await run_role_graph(run, model=planner, execute=execute,
        prepare_emotion=lambda current: dict(REFERENCE),
        finalize=lambda current: finalize_reply(current, model=speaker, instructions="根据真实回执回应。"), **options)
    assert len(dispatched) == planner.calls == speaker.calls == 1
    assert run.status == status and run.decision_draft == ""
    assert run.response == "这次动作尚未完成。" and run.emotion == "happy"
    assert run.steps[0]["result"]["final"] is True


@pytest.mark.asyncio
async def test_expression_replay_does_not_dispatch_again(options):
    dispatched = []
    for _ in range(2):
        run, model = new_run(), Model(dialogue())
        await run_role_graph(run, model=model,
            execute=lambda *args: dispatched.append(args) or {"result": "ok"},
            prepare_emotion=lambda current: dict(REFERENCE), **options)
        assert run.status == "ok" and run.steps[-1]["result"]["result"] == "ok"
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_reserved_expression_without_receipt_is_not_reissued(options):
    from cloud.agents.common.context import thread_id_for
    run = new_run()
    options["effect_ledger"].reserve(run.agent_id, thread_id_for(run.agent_id, run.session_id),
        run.run_id, run.run_id + ":emotion", "apply_training", REFERENCE)
    await run_role_graph(run, model=Model(dialogue()),
        execute=lambda *_: pytest.fail("uncertain prior dispatch must not repeat"),
        prepare_emotion=lambda current: dict(REFERENCE), **options)
    assert run.status == "unknown" and run.decision_draft == ""
    assert run.steps[0]["result"]["error"] == "prior_dispatch_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["none", "unavailable", "neutral", "question", "hidden_tool"])
async def test_unmatched_or_ineligible_expression_does_not_act(options, kind):
    prepared = []

    def prepare(current):
        prepared.append(current.emotion)
        if kind == "unavailable":
            raise ValueError("training_unavailable")
        return None

    if kind == "hidden_tool":
        options["tool_schemas"] = {}
    run = new_run()
    model = Model(dialogue(kind="question" if kind == "question" else "final",
                           emotion="neutral" if kind == "neutral" else "happy"))
    await run_role_graph(run, model=model,
        execute=lambda *_: pytest.fail("ineligible expression must not dispatch"),
        prepare_emotion=prepare, **options)
    assert run.steps == [] and model.calls == 1
    assert run.status == ("needs_clarification" if kind == "question" else "ok")
    assert prepared == (["happy"] if kind in {"none", "unavailable"} else [])


@pytest.mark.asyncio
async def test_action_final_reply_cannot_append_an_expression(options):
    run = new_run()
    model = Model(AIMessage(content="", tool_calls=[{
        "name": "apply_training", "args": dict(REFERENCE), "id": "explicit-action"}]))
    speaker = Model(dialogue())
    await run_role_graph(run, model=model, execute=lambda *_: {"result": "ok"},
        prepare_emotion=lambda *_: pytest.fail("final=true forbids new expression selection"),
        finalize=lambda current: finalize_reply(current, model=speaker, instructions="回应真实结果。"), **options)
    assert model.calls == speaker.calls == len(run.steps) == 1
    assert run.emotion == "happy" and run.emotion_step_id == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["limit", "invalid", "expired"])
async def test_expression_pre_dispatch_stop_discards_draft(options, failure):
    run = new_run()

    def prepare(current):
        if failure == "expired":
            current.deadline = time.time() - 1
        return {**REFERENCE, "revision": "wrong"} if failure == "invalid" else dict(REFERENCE)

    await run_role_graph(run, model=Model(dialogue()),
        execute=lambda *_: pytest.fail("preflight failure must not dispatch"),
        prepare_emotion=prepare, max_effects=0 if failure == "limit" else 6, **options)
    assert run.decision_draft == "" and run.steps == []
    assert run.status == {"limit": "blocked", "invalid": "rejected", "expired": "timeout"}[failure]
