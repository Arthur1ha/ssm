"""Activity handoff regressions with real Graph step ordering and no devices."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.messages import role_messages
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.steward.replies import finalize_steward_reply
from cloud.agents.steward.tools import ONCE_PER_RUN_TOOLS, STEWARD_TOOL_SCHEMAS, make_executor, transport


@pytest.fixture
def handoff(monkeypatch):
    activity = {"activity_id": "act_handoff", "status": "active", "devices": {
        "go2": {"status": "joined"}, "esp32_desk_led": {"status": "joined"}}}
    messenger = SimpleNamespace(send_message=Mock(return_value={
        "result": "accepted", "message_id": "handoff_message"}))
    monkeypatch.setattr(transport, "_messenger", messenger, raising=False)
    monkeypatch.setattr(transport, "do_start_activity", Mock(return_value={"activity": activity}))
    context = {"origin_kind": "user_delegated", "space_id": "main",
               "session_id": "session", "event_id": "evt_handoff", "user_revision": 1}
    run = GraphRun("handoff", "session", "让灯邀请狗玩猜拳", time.time() + 30,
                   agent_id="cloud_orchestrator")
    return SimpleNamespace(activity=activity, messenger=messenger, context=context, run=run)


async def test_first_handoff_passes_after_graph_appends_current_pending_step(handoff, tmp_path):
    h = handoff

    class Model(JsonReplyModel):
        def __init__(self):
            self.inputs = []

        def bind_tools(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, messages):
            self.inputs.append(messages)
            if len(self.inputs) == 1:
                return AIMessage(content="", tool_calls=[{
                    "name": "start_activity", "args": {"final": False}, "id": "start"}])
            if len(self.inputs) == 2:
                return AIMessage(content="", tool_calls=[{
                    "name": "send_message", "args": {
                        "target": "esp32_desk_led", "content": h.run.goal}, "id": "handoff"}])
            return AIMessage(content="活动已启动，邀请意图已转达；是否开局还未确认。")

    model = Model()
    await run_role_graph(h.run, model=model, tool_schemas=STEWARD_TOOL_SCHEMAS,
        once_per_run_tools=ONCE_PER_RUN_TOOLS,
        build_messages=lambda current: role_messages("回应本轮结果", current.goal),
        execute=make_executor(h.run, h.context),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"),
        finalize=lambda current: finalize_steward_reply(
            current, model=model, instructions="简短报告已确认阶段，不把邀请送达当成开局。"))
    h.messenger.send_message.assert_called_once_with(
        "cloud_orchestrator", "esp32_desk_led", h.run.goal, None, activity_id="act_handoff")
    assert h.run.steps[-1]["result"] == {
        "result": "ok", "device_result": "accepted", "message_id": "handoff_message", "final": True}
    assert h.run.status == "ok"
    assert "还未确认" in h.run.response


async def test_duplicate_start_reuses_activity_without_another_start_or_failure(handoff, tmp_path):
    h = handoff

    class Model(JsonReplyModel):
        def __init__(self):
            self.calls = 0

        def bind_tools(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            self.calls += 1
            if self.calls <= 2:
                return AIMessage(content="", tool_calls=[{"name": "start_activity",
                    "args": {"final": False}, "id": f"start-{self.calls}"}])
            return AIMessage(content="活动安排已交给参与设备。")

    model = Model()
    await run_role_graph(h.run, model=model, tool_schemas=STEWARD_TOOL_SCHEMAS,
        once_per_run_tools=ONCE_PER_RUN_TOOLS,
        build_messages=lambda current: role_messages("回应本轮结果", current.goal),
        execute=make_executor(h.run, h.context),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"),
        finalize=lambda current: finalize_steward_reply(
            current, model=model, instructions="简短报告本轮结果。"))

    transport.do_start_activity.assert_called_once()
    assert h.run.status == "ok" and len(h.run.steps) == 1
    assert h.run.steps[0]["result"]["activity"] == h.activity
    assert model.calls == 3  # Two decisions, then one closing reply without tools.


async def test_start_exception_remains_unknown_and_new_call_cannot_redispatch(handoff, tmp_path, monkeypatch):
    h = handoff
    monkeypatch.setattr(transport, "do_start_activity", Mock(side_effect=TimeoutError))

    class Model:
        def bind_tools(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            return AIMessage(content="", tool_calls=[{
                "name": "start_activity", "args": {"final": False}, "id": "start"}])

    execute = make_executor(h.run, h.context)
    await run_role_graph(h.run, model=Model(), tool_schemas=STEWARD_TOOL_SCHEMAS,
        once_per_run_tools=ONCE_PER_RUN_TOOLS,
        build_messages=lambda current: role_messages("回应本轮结果", current.goal),
        execute=execute, checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))

    assert h.run.status == "unknown" and len(h.run.steps) == 1
    repeated = await execute("start_activity", {}, "new-call")
    assert repeated["result"] == "unknown"
    transport.do_start_activity.assert_called_once()


async def test_signed_user_intent_can_handoff_after_starting_activity(handoff, tmp_path, monkeypatch):
    from cloud.mqtt_signing import sign_message
    from cloud.orchestrator.trusted_context import parse_trusted_intent

    h = handoff
    monkeypatch.setenv("SSM_USER_GOALS_DIR", str(tmp_path / "goals"))
    from cloud.space.user_goals import get_goal_registry
    revision = get_goal_registry().advance("session", "evt_handoff")
    now = time.time()
    payload = sign_message('ssm/intent/session', {
        'text': h.run.goal, 'event_id': 'evt_handoff', 'user_revision': 1, 'goal_revision': revision,
        'origin_kind': 'untrusted_value',
        'context': {'session_id': 'session', 'actor': 'admin', 'persist': False,
                    'space_id': 'main', 'issued_at': now, 'expires_at': now + 60}},
        signing_secret='handoff-fixture-signing-key', now=now)
    context = parse_trusted_intent(payload, 'session',
        signing_secret='handoff-fixture-signing-key', now=now)
    assert context['origin_kind'] == 'user_delegated'
    h.run.steps = [
        {'tool': 'start_activity', 'result': {'result': 'ok', 'activity': h.activity}},
        {'step_id': 'current', 'tool': 'send_message', 'result': None}]
    result = await make_executor(h.run, context)('send_message', {
        'target': 'go2', 'content': h.run.goal}, 'current')
    assert result['result'] == 'accepted'
    h.messenger.send_message.assert_called_once()


@pytest.mark.parametrize("prior_result,blocked", [
    ("accepted", True), ("unknown", True), ("timeout", True), (None, True),
    ("rejected", False), ("blocked", False), ("error", False), ("cancelled", False),
])
async def test_handoff_deduplication_distinguishes_delivery_from_failed_attempt(handoff, prior_result, blocked):
    h = handoff
    h.run.steps = [
        {"tool": "start_activity", "result": {"result": "ok", "activity": h.activity}},
        {"step_id": "prior", "tool": "send_message", "result": {"result": prior_result}},
        {"step_id": "current", "tool": "send_message", "result": None},
    ]
    result = await make_executor(h.run, h.context)("send_message", {
        "target": "esp32_desk_led", "content": h.run.goal}, "current")
    assert result["result"] == ("rejected" if blocked else "accepted")
    assert h.messenger.send_message.call_count == (0 if blocked else 1)


@pytest.mark.parametrize("invalid", ["origin", "membership"])
async def test_first_handoff_still_requires_trusted_user_and_joined_target(handoff, invalid):
    h = handoff
    if invalid == "origin":
        h.context["origin_kind"] = "agent_autonomous"
    else:
        h.activity["devices"]["esp32_desk_led"]["status"] = "failed"
    h.run.steps = [
        {"tool": "start_activity", "result": {"result": "ok", "activity": h.activity}},
        {"step_id": "current", "tool": "send_message", "result": None},
    ]
    result = await make_executor(h.run, h.context)("send_message", {
        "target": "esp32_desk_led", "content": h.run.goal}, "current")
    assert result == {"result": "rejected", "error": "activity_message_denied"}
    h.messenger.send_message.assert_not_called()
