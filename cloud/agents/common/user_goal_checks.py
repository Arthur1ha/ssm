"""Superseding a real Graph never replays its effects or blocks a new target."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.space.user_goals import GoalRegistry, GoalSuperseded, get_goal_registry


@pytest.fixture
def goals(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_USER_GOALS_DIR", str(tmp_path / "goals"))
    return get_goal_registry()


class Model:
    def __init__(self, reply=None):
        self.reply = reply or AIMessage(content='{"kind":"final","text":"新的目标。"}')

    def bind_tools(self, *_args, **_kwargs):
        return self

    async def ainvoke(self, _messages):
        return self.reply


def make_run(revision, run_id="old"):
    return GraphRun(run_id, "session", run_id, time.time() + 60,
                    agent_id="cloud_orchestrator", goal_revision=revision)


async def graph(run, model, tmp_path, *, execute=None, schemas=None):
    return await run_role_graph(run, model=model, tool_schemas=schemas or {},
        build_messages=lambda current: [HumanMessage(content=current.goal)],
        execute=execute or (lambda *_args: {"result": "ok"}),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))


def test_goal_registry_is_shared_and_import_does_not_create_assets(goals):
    assert not goals.directory.exists()
    first = goals.advance("session", "one")
    independent = GoalRegistry(goals.directory)
    assert independent.is_current(first)
    second = independent.advance("session", "two")
    assert second > first and not goals.is_current(first)
    with pytest.raises(GoalSuperseded):
        goals.check(first)


@pytest.mark.parametrize("revision", ["missing", None, 0, -1, True, "1"])
def test_signed_intent_rejects_missing_or_invalid_goal_revision(revision):
    from cloud.mqtt_signing import sign_message
    from cloud.orchestrator.trusted_context import parse_trusted_intent, TrustedContextError
    secret = "goal-revision-contract-signing-secret"
    payload = {"text": "开灯", "context": {
        "session_id": "session", "actor": "visitor", "persist": False,
        "space_id": "main", "issued_at": 900, "expires_at": 1100}}
    if revision != "missing":
        payload["goal_revision"] = revision
    signed = sign_message("ssm/intent/session", payload, signing_secret=secret,
                          internal_token="", now=1000)
    with pytest.raises(TrustedContextError, match="goal_revision"):
        parse_trusted_intent(signed, "session", signing_secret=secret, token="", now=1000)


def test_trusted_http_dispatch_keeps_goal_and_rejects_stale_calls(goals, monkeypatch):
    from unittest.mock import MagicMock
    from cloud.orchestrator import tools
    revision = goals.advance("session", "http-command")
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "goal-http-contract-service-token")
    response = MagicMock()
    response.is_redirect = False
    response.json.return_value = {"result": "ok"}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(tools.requests, "post", post)
    body = {"session_id": "session", "actor": "visitor", "persist": False,
            "space_id": "main", "message": "hello", "goal_revision": revision}
    assert tools.do_http_dispatch("/api/go2/chat", body, 1)["result"] == "ok"
    assert post.call_args.kwargs["headers"]["X-SSM-Goal-Revision"] == str(revision)
    goals.advance("session", "replacement")
    with pytest.raises(GoalSuperseded):
        tools.do_http_dispatch("/api/go2/chat", body, 1)
    post.assert_called_once()


async def test_new_graph_does_not_wait_for_old_model(goals, tmp_path):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Slow(Model):
        async def ainvoke(self, _messages):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    old = make_run(goals.advance("session", "old"))
    work = asyncio.create_task(graph(old, Slow(), tmp_path))
    await asyncio.wait_for(entered.wait(), 2)
    new = make_run(goals.advance("session", "new"), "new")
    completed = await asyncio.wait_for(graph(new, Model(), tmp_path), 2)
    await asyncio.wait_for(work, 2)
    assert cancelled.is_set()
    assert old.status == "cancelled" and old.response == ""
    assert completed.status == "ok" and completed.response == "新的目标。"


async def test_blocking_physical_wait_is_abandoned_without_replay(goals, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def dispatch(name, _params, _step):
        calls.append(name)
        entered.set()
        release.wait(5)
        return {"result": "ok"}

    old = make_run(goals.advance("session", "old"))
    model = Model(AIMessage(content="", tool_calls=[{
        "name": "move", "args": {}, "id": "physical-one"}]))
    work = asyncio.create_task(graph(old, model, tmp_path, execute=dispatch,
        schemas={"move": {"title": "move", "type": "object", "properties": {}}}))
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(.01)
        new = make_run(goals.advance("session", "new"), "new")
        await asyncio.wait_for(graph(new, Model(), tmp_path), 2)
        await asyncio.wait_for(work, 2)
        assert calls == ["move"]
        assert old.status == "cancelled"
        assert old.steps[0]["dispatch_status"] == "unknown"
        assert old.steps[0]["result"]["result"] == "unknown"
    finally:
        release.set()


async def test_success_received_when_goal_changes_is_preserved(goals, tmp_path):
    old = make_run(goals.advance("session", "old"))

    async def execute(*_args):
        goals.advance("session", "new")
        return {"result": "ok", "confirmation": "device_receipt"}

    model = Model(AIMessage(content="", tool_calls=[{
        "name": "move", "args": {}, "id": "physical-success"}]))
    await graph(old, model, tmp_path, execute=execute,
        schemas={"move": {"title": "move", "type": "object", "properties": {}}})
    assert old.status == "cancelled" and old.response == ""
    assert old.steps[0]["result"]["result"] == "ok"
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    saved = saver.get_tuple({"configurable": {"thread_id": thread_id_for(old.agent_id, old.session_id)}})
    assert saved.checkpoint["channel_values"]["run"]["steps"][0]["result"]["result"] == "ok"


async def test_superseded_wait_preserves_receipt_already_in_ledger(goals, tmp_path):
    from cloud.agents.common.effects import current_effect
    entered = asyncio.Event()
    receipt = {"result": "ok", "confirmation": "device_receipt"}
    old = make_run(goals.advance("session", "old"))

    async def execute(*_args):
        ledger, identity = current_effect()
        ledger.mark_result(*identity, receipt)
        entered.set()
        await asyncio.Event().wait()

    model = Model(AIMessage(content="", tool_calls=[{
        "name": "move", "args": {}, "id": "physical-received"}]))
    task = asyncio.create_task(graph(old, model, tmp_path, execute=execute,
        schemas={"move": {"title": "move", "type": "object", "properties": {}}}))
    await asyncio.wait_for(entered.wait(), 2)
    goals.advance("session", "replacement")
    await asyncio.wait_for(task, 2)
    assert old.status == "cancelled" and old.response == ""
    assert old.steps[0]["dispatch_status"] == "observed"
    assert old.steps[0]["result"] == receipt
    saved = FileCheckpointSaver(tmp_path / "checkpoints").get_tuple({"configurable": {
        "thread_id": thread_id_for(old.agent_id, old.session_id)}})
    assert saved.checkpoint["channel_values"]["run"]["steps"][0]["result"] == receipt


async def test_waiting_graph_lock_cancels_before_it_acquires(goals, tmp_path):
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    revision = goals.advance("session", "old")
    thread = thread_id_for("cloud_orchestrator", "session")
    with saver.run_lock(thread):
        async def wait():
            async with saver.async_run_lock(thread, goal_revision=revision):
                pytest.fail("obsolete run acquired the lock")
        task = asyncio.create_task(wait())
        await asyncio.sleep(.02)
        goals.advance("session", "new")
        with pytest.raises(GoalSuperseded):
            await asyncio.wait_for(task, 1)


async def test_resume_keeps_goal_and_stale_resume_is_rejected(goals, monkeypatch):
    from cloud.api.intents import begin_user_goal
    from fastapi import HTTPException
    session = {"session_id": "session", "actor": "admin", "expires_at": time.time() + 60}
    revision = goals.advance("session", "original")
    run = SimpleNamespace(goal_revision=revision)
    resumed = await begin_user_goal(session, "original", resume_run=run)
    assert resumed["goal_revision"] == revision and goals.current()["revision"] == revision
    goals.advance("session", "replacement")
    with pytest.raises(HTTPException, match="user_goal_superseded"):
        await begin_user_goal(session, "original", resume_run=run)


async def test_old_joystick_stop_cannot_renew_or_stop_new_goal(goals, monkeypatch):
    from cloud.api import intents
    from fastapi import HTTPException
    calls = []

    async def internal(path, method, session, body):
        calls.append((path, session["goal_revision"], body))
        return {"result": "accepted"}

    monkeypatch.setattr(intents, "_internal_request", internal)
    monkeypatch.setattr(intents, "_velocity_goal_scopes", {})
    session = {"session_id": "session", "actor": "admin", "expires_at": time.time() + 60}
    await intents._dispatch_go2("velocity", {
        "controller_id": "gesture-one", "sequence": 1, "vx": .1}, session)
    previous = goals.current()["revision"]
    replacement = goals.advance("session", "new-chat")
    with pytest.raises(HTTPException, match="user_goal_superseded"):
        await intents._dispatch_go2("velocity", {
            "controller_id": "gesture-one", "sequence": 2, "vx": 0}, session)
    assert goals.current()["revision"] == replacement and len(calls) == 1
    assert calls[0][1] == previous
    ignored = await intents._dispatch_go2("velocity", {
        "controller_id": "unknown-old", "sequence": 7, "vx": 0}, session)
    assert ignored["ignored"] and len(calls) == 1


async def test_stop_unknown_does_not_reject_new_lamp_or_conversation(goals, monkeypatch):
    from cloud.api.intents import begin_user_goal
    from unittest.mock import AsyncMock
    monkeypatch.setattr("cloud.go2.service.prepare_user_goal", AsyncMock(return_value={
        "result": "blocked", "physical_stop": "unknown"}))
    session = {"session_id": "session", "actor": "admin", "expires_at": time.time() + 60}
    trusted = await begin_user_goal(session, "lamp-chat")
    assert goals.is_current(trusted["goal_revision"])
    assert trusted["go2_handoff"]["physical_stop"] == "unknown"


async def test_old_post_request_cannot_cancel_current_target(goals):
    from cloud.api.intents import begin_user_goal
    from fastapi import HTTPException
    session = {"session_id": "session", "actor": "admin", "expires_at": time.time() + 60}
    first = await begin_user_goal(session, "old-request", prepare=False)
    second = await begin_user_goal(session, "new-request", prepare=False)
    with pytest.raises(HTTPException, match="user_goal_superseded"):
        await begin_user_goal(session, "old-request", prepare=False)
    assert goals.current()["revision"] == second["goal_revision"]
    assert second["goal_revision"] != first["goal_revision"]


async def test_unversioned_resume_cannot_reactivate_old_run(goals):
    from cloud.api.intents import begin_user_goal
    from fastapi import HTTPException
    session = {"session_id": "session", "actor": "admin", "expires_at": time.time() + 60}
    goals.advance("session", "new")
    with pytest.raises(HTTPException, match="user_goal_superseded"):
        await begin_user_goal(session, "legacy-run", resume_run=SimpleNamespace(goal_revision=0))


async def test_stale_implicit_question_starts_a_new_target(goals, monkeypatch):
    from cloud.api import intents
    from unittest.mock import AsyncMock, Mock
    revision = goals.advance("session", "old-question")
    goals.advance("other-page", "another-target")
    pending = {"session_id": "session", "run_id": "old-question",
               "status": "executing", "goal_revision": revision}
    monkeypatch.setattr("cloud.agents.common.checkpoint.FileCheckpointSaver", lambda: SimpleNamespace(
        get_tuple=lambda _config: SimpleNamespace(checkpoint={"channel_values": {
            "run": pending, "decision": {"kind": "ask"}}})))
    monkeypatch.setattr("cloud.go2.service.prepare_user_goal", AsyncMock(return_value={"result": "ok"}))
    monkeypatch.setattr("cloud.api.activity.user_intervention", AsyncMock(return_value=1))
    publish = Mock()
    monkeypatch.setattr(intents, "_mqtt_publish_signed", publish)
    session = {"session_id": "session", "actor": "admin", "issued_at": time.time() - 1,
               "expires_at": time.time() + 60}
    result = await intents.create_intent(intents.IntentRequest(text="新的事情"), session)
    payload = publish.call_args.args[1]
    assert "resume_run_id" not in payload
    assert result["goal_revision"] != revision


async def test_zero_sequence_cannot_advance_goal(goals):
    from cloud.api import intents
    from fastapi import HTTPException
    revision = goals.advance("session", "current")
    session = {"session_id": "session", "actor": "admin", "expires_at": time.time() + 60}
    with pytest.raises(HTTPException, match="controller_id and sequence"):
        await intents._dispatch_go2("velocity", {
            "controller_id": "bad", "sequence": 0, "vx": .1}, session)
    assert goals.is_current(revision)


async def test_actual_peer_callback_captures_goal_at_receipt(goals):
    # main.py starts MQTT and its process loop at import. Compile its actual
    # callback/handler definitions so this integration stays device-free.
    import ast
    from pathlib import Path
    from unittest.mock import Mock
    source = Path(__file__).resolve().parents[2] / "orchestrator" / "main.py"
    tree = ast.parse(source.read_text())
    definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name in {"_queue_agent_message", "_handle_agent_message"}]
    submitted = []
    messenger = SimpleNamespace(send_result=Mock())
    env = {"asyncio": SimpleNamespace(run_coroutine_threadsafe=lambda coroutine, loop: submitted.append(coroutine)),
           "_intent_loop": object(), "_agent_messenger": messenger}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(source), "exec"), env)
    goals.advance("session", "first")
    env["_queue_agent_message"]({"message_id": "note", "sender": "lamp", "content": "hello"})
    goals.advance("session", "replacement")
    await submitted[0]
    messenger.send_result.assert_called_once_with("note", "cancelled", reason="newer_user_goal")


def test_superseded_between_reserve_and_publish_records_not_dispatched(goals, tmp_path, monkeypatch):
    from cloud.esp32 import physical_tasks
    from cloud.esp32.state import ESP32State
    from cloud.esp32.tool_executor import execute_physical
    from cloud.space.execution import ExecutionContext
    from unittest.mock import Mock
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path / "physical")
    monkeypatch.setenv("SSM_TRAINING_DIR", str(tmp_path / "training"))
    original_reserve = physical_tasks.reserve

    def reserve(*args, **kwargs):
        result = original_reserve(*args, **kwargs)
        goals.advance("session", "replacement")
        return result

    monkeypatch.setattr(physical_tasks, "reserve", reserve)
    revision = goals.advance("session", "physical")
    context = ExecutionContext("session", "admin", False, expires_at=time.time() + 60,
                               goal_revision=revision)
    dispatch = Mock()
    with pytest.raises(GoalSuperseded):
        execute_physical(state=ESP32State(), unit_id="lamp", action="SET_STATE",
            params={"state": "ON"}, context=context, dispatch=dispatch, wait=False,
            task_id="not-published")
    dispatch.assert_not_called()
    saved = physical_tasks.observed_result("not-published")
    assert saved["result"] == "cancelled" and saved["dispatch_status"] == "not_dispatched"


async def test_old_multiaction_teaching_stops_before_next_dispatch(goals, tmp_path, monkeypatch):
    from cloud.agents.common.training import service, store, catalog
    from cloud.agents.common.training.workflow import run_training_workflow
    from cloud.space.execution import ExecutionContext
    for key in ("SSM_CHECKPOINT_DIR", "SSM_EFFECT_LEDGER_DIR"):
        monkeypatch.setenv(key, str(tmp_path / key))
    record = {"teaching_id": "lesson", "revision": 1, "desired_status": "enabled",
              "capability_hash": "hash", "definition": {"steps": [{}, {}]}}
    monkeypatch.setattr(store, "list_records", lambda *_a, **_k: [record])
    monkeypatch.setattr(service, "_target", lambda *_a: {})
    monkeypatch.setattr(catalog, "catalog_for", lambda _card: {"hash": "hash"})
    monkeypatch.setattr(service, "resolve_training_step", lambda _record, _catalog, index, *_a: {
        "transition_id": f"t{index}", "action": "SET_STATE", "params": {"state": "ON"}})
    calls = []

    def execute(action, params, context, request_id):
        calls.append(request_id)
        goals.advance("session", "replacement")
        return {"result": "ok"}

    revision = goals.advance("session", "teaching")
    context = ExecutionContext("session", "admin", False, expires_at=time.time() + 60,
                               goal_revision=revision)
    result = await asyncio.wait_for(run_training_workflow({"unit_id": "lamp", "training_ref": "lesson",
        "training_revision": 1, "record": record}, context, run_id="teaching-run",
        snapshot=lambda: {}, execute=execute), 3)
    assert result["result"] == "cancelled" and len(calls) == 1
    assert result["steps"][0]["result"]["result"] == "ok"
