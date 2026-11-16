"""Go2 social emotions use confirmed teachings with durable live admission."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.go2.workflows.social import build_social_graph
from cloud.peers.go2_social_invitation_checks import social_bus


class Model:
    def __init__(self, **changes):
        self.decision = {"decision": "send_message", "target": "lamp", "content": "真开心！",
                         "emotion": "happy", **changes}

    def with_structured_output(self, *_args, **_kwargs):
        return self

    async def ainvoke(self, _messages):
        return self.decision


def state():
    return {"run_id": "social-expression", "activity_id": "act_test", "active": True,
            "connected": True, "opportunity": {"event_ids": ["new-observation"]},
            "peers": [{"unit_id": "lamp"}],
            "playable_games": [{"target": "lamp", "game_id": "color_hunt"}]}


def graph(tmp_path, monkeypatch, *, model=None, delivery="accepted", callback=None):
    monkeypatch.setattr("cloud.agents.go2.workflows.social.agent_names", lambda: {})
    monkeypatch.setattr("cloud.peers.games.game_is_playable", lambda *_: True)
    return build_social_graph(model or Model(),
        profile_loader=lambda: {"prompt": "小狗", "traits": {"extraversion": 70}},
        can_send=lambda *_: True, send_message=AsyncMock(return_value={"result": delivery}),
        effect_ledger=FileEffectLedger(tmp_path / "effects"), express_emotion=callback)


@pytest.mark.asyncio
@pytest.mark.parametrize("physical_status", ["ok", "blocked", "unknown"])
async def test_social_message_and_expression_results_are_separate_and_durable(tmp_path, monkeypatch, physical_status):
    callback = AsyncMock(return_value={"result": physical_status, "task_id": "real-action"})
    compiled = graph(tmp_path, monkeypatch, callback=callback)
    first = await compiled.ainvoke(state())
    second = await compiled.ainvoke(state())
    assert first["status"] == second["status"] == "accepted"
    assert first["expression"]["result"] == physical_status
    assert second["expression"]["result"] == physical_status
    callback.assert_awaited_once()
    assert callback.call_args.args[0]["decision"]["emotion"] == "happy"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["game_invite", "skip", "neutral", "unknown", "rejected"])
async def test_social_expression_requires_successful_ordinary_emotional_message(tmp_path, monkeypatch, kind):
    model = Model(**({"interaction_kind": "game_invite", "game_id": "color_hunt"} if kind == "game_invite"
                    else {"decision": "skip"} if kind == "skip"
                    else {"emotion": "neutral"} if kind == "neutral" else {}))
    callback = AsyncMock(side_effect=AssertionError("must not express"))
    compiled = graph(tmp_path, monkeypatch, model=model, callback=callback,
                     delivery=kind if kind in {"unknown", "rejected"} else "accepted")
    output = await compiled.ainvoke(state())
    assert "expression" not in output
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_expression_reservation_never_reexecutes(tmp_path, monkeypatch):
    ledger = FileEffectLedger(tmp_path / "effects")
    ledger.reserve("go2", "go2:social:social-expression", "social-expression", "emotion",
                   "express_emotion", {"emotion": "happy", "target": "lamp"})
    callback = AsyncMock(side_effect=AssertionError("uncertain prior action must not repeat"))
    output = await graph(tmp_path, monkeypatch, callback=callback).ainvoke(state())
    assert output["expression"] == {"result": "unknown", "error": "prior_expression_unconfirmed"}
    callback.assert_not_awaited()


def setup_drive(h, monkeypatch):
    from cloud.go2.mode import free_explore
    runtime = SimpleNamespace(data={"epoch": "body-epoch", "autonomy_mode": "free_explore"},
                              active_reason=lambda: None)
    monkeypatch.setattr(free_explore, "runtime_for", lambda _: runtime)
    monkeypatch.setattr(free_explore, "get_text_llm", lambda: Model(target="esp32_desk_led"))
    select = Mock(return_value={"unit_id": "go2", "teaching_id": "happy-rule", "revision": 2, "overrides": {}})
    prepare = AsyncMock(return_value={"unit_id": "go2"})
    publish = Mock()
    monkeypatch.setattr("cloud.agents.common.training.context.emotion_training_call", select)
    monkeypatch.setattr("cloud.agents.common.training.tools.prepare_training_application", prepare)
    monkeypatch.setattr("cloud.api.intents.publish_agent_event", publish)
    return runtime, select, prepare, publish


def test_drive_social_expression_uses_autonomous_context_and_original_behavior(social_bus, monkeypatch):
    h = social_bus
    runtime, select, prepare, publish = setup_drive(h, monkeypatch)
    calls = []

    async def behavior(prepared, context, *, run_id, availability):
        calls.append((prepared, context, run_id))
        # The message itself creates a pending conversation; it must not block
        # the matching expression belonging to this same social decision.
        assert h.bus.has_pending_conversation("go2", "esp32_desk_led")
        assert availability(context)
        return {"result": "ok", "training_ref": "happy-rule"}

    monkeypatch.setattr("cloud.go2.service.run_training_behavior", behavior)
    asyncio.run(h.drive._evaluate_social())
    assert len(calls) == 1
    _, execution, run_id = calls[0]
    assert execution.origin_kind == "agent_autonomous" and execution.actor == "visitor"
    assert execution.persist is False and execution.session_id == "act_social"
    assert run_id.endswith(":emotion")
    assert select.call_args.args[0].emotion == "happy"
    assert select.call_args.kwargs["origin_kind"] == "agent_autonomous"
    assert publish.call_args.kwargs["status"] == "ok"
    assert publish.call_args.kwargs["training_ref"] == "happy-rule"


@pytest.mark.parametrize("change", ["interrupt", "question", "generation", "owner", "connection", "mode", "epoch", "event"])
def test_drive_expression_rechecks_live_admission_between_steps(social_bus, monkeypatch, change):
    from cloud.go2.mode import free_explore
    h = social_bus
    runtime, _, _, _ = setup_drive(h, monkeypatch)
    checked = []

    async def behavior(_prepared, context, *, run_id, availability):
        assert availability(context)
        if change == "interrupt":
            h.drive.user_interrupt = True
        elif change == "question":
            h.drive._pending_question_id = "new-question"
        elif change == "generation":
            h.drive._generation += 1
        elif change == "owner":
            h.drive.owner_session_id = "new-owner"
        elif change == "connection":
            free_explore.go2.is_connected = False
        elif change == "mode":
            runtime.data["autonomy_mode"] = "manual"
        elif change == "epoch":
            runtime.data["epoch"] = "new-body-epoch"
        else:
            h.drive._events.append({"event_id": "new-event"})
        checked.append(availability(context))
        return {"result": "cancelled"}

    monkeypatch.setattr("cloud.go2.service.run_training_behavior", behavior)
    asyncio.run(h.drive._evaluate_social())
    assert checked == [False]


def test_drive_without_matching_teaching_keeps_message_only(social_bus, monkeypatch):
    h = social_bus
    _, select, prepare, publish = setup_drive(h, monkeypatch)
    select.return_value = None
    behavior = AsyncMock()
    monkeypatch.setattr("cloud.go2.service.run_training_behavior", behavior)
    asyncio.run(h.drive._evaluate_social())
    assert len(h.sent) == 1
    prepare.assert_not_awaited()
    behavior.assert_not_awaited()
    publish.assert_not_called()
