"""Cross-session teaching context against real cards and isolated confirmed data."""

from copy import deepcopy
import json
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.common.training import service, store
from cloud.agents.common.training.catalog import catalog_for
from cloud.agents.common.training.context import decision_context
from cloud.agents.common.training.models import ApplyTraining
from cloud.agents.common.training.tools import prepare_training_application
from cloud.esp32.capability_chain_checks import card as lamp_card
from cloud.go2.card import _build_go2_card
from cloud.space.execution import ExecutionContext


PURPLE = {"r": 160, "g": 50, "b": 255, "brightness": 20}


@pytest.fixture
def teachings(monkeypatch, tmp_path):
    from cloud.agents.lamp import prompts as lamp
    from cloud.agents.go2 import prompts as robot

    monkeypatch.setenv("SSM_TRAINING_DIR", str(tmp_path / "training"))
    cards = {"lamp": lamp_card(), "go2": _build_go2_card()}
    monkeypatch.setattr(service, "_targets", lambda space: {
        card["unit_id"]: card for card in cards.values()} if space == "main" else {})
    monkeypatch.setattr(service, "_emit", lambda *_a, **_k: None)
    profile = {"agent_id": "test", "name": "测试", "prompt": "保持当前人格",
               "traits": {"extraversion": 50}}
    for module in (lamp, robot):
        monkeypatch.setattr(module, "get_profile", lambda: profile)
        monkeypatch.setattr(module, "agent_names", lambda: {})
    monkeypatch.setattr(lamp, "get_space_context", lambda *_a, **_k: {"peers": []})
    monkeypatch.setattr(robot, "prompt_space_context", lambda: {})
    monkeypatch.setattr(robot.go2, "state_snapshot", lambda: {"body_state": "lying"})
    agent = SimpleNamespace(_state=SimpleNamespace(actuator_snapshot=lambda: {
        cards["lamp"]["unit_id"]: {"state": {"ism": "OFF", "brightness": 0, "ts": time.time()}}}))
    session = {"session_id": "teaching-session", "space_id": "main", "actor": "admin",
               "expires_at": time.time() + 300}
    session["training_source"] = service.UserText(session["session_id"], "原会话的教学原话")

    def definition(role):
        catalog = catalog_for(cards[role])
        if role == "lamp":
            steps = [{"transition_ids": [edge["id"] for edge in catalog["transitions"]
                                         if edge["action"] == "SET_COLOR"], "defaults": PURPLE}]
            situation = "阅读、看书时"
        else:
            stand, hello = [next(edge for edge in catalog["transitions"]
                                if edge["trigger"] == trigger and edge["src"] == src)
                            for trigger, src in (("StandUp", "lying"), ("Hello", "standing"))]
            steps = [{"transition_ids": [stand["id"]], "goal_state": "standing"},
                     {"transition_ids": [hello["id"]], "conditions": [
                         {"field": "body_state", "op": "eq", "value": "standing"}]}]
            situation = "用户想打个招呼时"
        return {"steps": steps, "origins": ["user_delegated"], "situation": situation}

    def save(role, *, confirmed=True, definition_override=None):
        card = cards[role]
        proposal = service.propose({"operation": "create", "unit_id": card["unit_id"],
            "name": "阅读用紫色" if role == "lamp" else "先起身再招呼",
            "effect_text": "沿用已确认的颜色" if role == "lamp" else "先站起来，再挥手打招呼",
            "definition": definition_override or definition(role),
            "expected_revision": 0, "capability_hash": catalog_for(card)["hash"]}, session)
        return service.confirm(proposal["proposal_id"], 0, session) if confirmed else proposal

    def messages(role, session_id="new-session", current=None):
        run = current or GraphRun("run-" + session_id, session_id,
                       "我想要阅读了" if role == "lamp" else "打个招呼吧", time.time() + 60,
                       origin_kind="user_delegated")
        result = (lamp.decision_messages(run, agent=agent, unit_id=cards[role]["unit_id"])
                  if role == "lamp" else robot.build_messages(run))
        data = next(message for message in result if message.name == "context")
        assert isinstance(data, HumanMessage)
        return result, json.loads(data.content.split("\n", 1)[1])

    return SimpleNamespace(cards=cards, save=save, definition=definition, messages=messages,
                           session=session)


@pytest.mark.parametrize("role", ["lamp", "go2"])
def test_new_session_sees_confirmed_teaching_and_current_state(teachings, role):
    proposal = teachings.save(role, confirmed=False)
    assert teachings.messages(role)[1]["training"]["records"] == []
    saved = service.confirm(proposal["proposal_id"], 0, teachings.session)
    assert saved["loaded_revision"] is None  # Device ACK is not a prerequisite for model context.
    for session_id in ("new-session", "another-session"):
        messages, context = teachings.messages(role, session_id)
        training = context["training"]
        assert training["status"] == "ok"
        record, = training["records"]
        assert record["teaching_id"] == saved["teaching_id"] and record["revision"] == 1
        assert record["definition"] == saved["definition"]
        assert record["effect_text"] == saved["effect_text"]
        assert "apply_training" in messages[0].content
        assert "原会话的教学原话" not in str(messages)
        assert not {"history", "source_text", "proposals"}.intersection(record)
        if role == "lamp":
            assert record["definition"]["steps"][0]["defaults"] == PURPLE
            assert context["lamp"][training["unit_id"]]["state"]["ism"] == "OFF"
        else:
            assert context["device_state"]["body_state"] == "lying"
            assert record["definition"]["steps"][1]["conditions"][0]["value"] == "standing"
            assert {edge["action"] for edge in training["transitions"]} == {"StandUp", "Hello"}


@pytest.mark.parametrize("role", ["lamp", "go2"])
def test_each_model_turn_refreshes_revision_disable_and_removal(teachings, role):
    saved = teachings.save(role)
    changed = store.commit(saved | {"name": "更新后的教导"}, expected_revision=1, proposal_id="updated")
    record, = teachings.messages(role)[1]["training"]["records"]
    assert record["revision"] == 2 and record["name"] == "更新后的教导"
    for status in ("disabled", "removed"):
        changed = store.commit(changed | {"desired_status": status},
                               expected_revision=changed["revision"], proposal_id=status)
        assert teachings.messages(role)[1]["training"]["records"] == []


@pytest.mark.parametrize("role", ["lamp", "go2"])
@pytest.mark.parametrize("change", ["capability", "definition", "origin"])
def test_incompatible_or_wrong_origin_rules_are_not_offered(teachings, role, change):
    saved = teachings.save(role)
    changed = deepcopy(saved)
    if change == "capability":
        changed["capability_hash"] = "0" * 64
    elif change == "definition":
        changed["definition"]["steps"][0]["transition_ids"] = ["obsolete_transition"]
    else:
        changed["definition"]["origins"] = ["agent_autonomous"]
    store.commit(changed, expected_revision=1, proposal_id="incompatible")
    context = teachings.messages(role)[1]["training"]
    assert context["records"] == [] and context["transitions"] == []


def test_context_cannot_read_another_space_and_reports_storage_failure(teachings, monkeypatch):
    teachings.save("lamp")
    unit = teachings.cards["lamp"]["unit_id"]
    assert decision_context(unit, space_id="other")["status"] == "unavailable"

    def unreadable(*_a, **_k):
        raise store.StorageError("training_storage_unreadable")

    monkeypatch.setattr(store, "list_records", unreadable)
    assert decision_context(unit) == {
        "unit_id": unit, "status": "unavailable", "records": [], "transitions": []}


def test_semantic_lighting_tasks_can_apply_saved_teaching_without_editing_it():
    from cloud.agents.lamp.tools.schemas import visible_schemas

    schemas = visible_schemas(lighting_only=True, card=lamp_card())
    assert "apply_training" in schemas
    assert "propose_training" not in schemas and "list_training" not in schemas


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["lamp", "go2"])
async def test_new_session_selects_saved_reference_without_reconstructing_parameters(
        teachings, role, monkeypatch, tmp_path):
    for key in ("SSM_CHECKPOINT_DIR", "SSM_EFFECT_LEDGER_DIR"):
        monkeypatch.setenv(key, str(tmp_path / key))

    saved = teachings.save(role)
    card = teachings.cards[role]
    catalog = catalog_for(card)
    context = ExecutionContext("new-dialogue", "admin", True, expires_at=int(time.time()) + 120)
    prepared_calls = []

    class ContextModel(JsonReplyModel):
        calls = 0

        def bind_tools(self, *_a, **_k):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls > 1:
                return AIMessage(content="已经按之前教的做好了。")
            data = next(message for message in messages if message.name == "context")
            record, = json.loads(data.content.split("\n", 1)[1])["training"]["records"]
            assert record["teaching_id"] == saved["teaching_id"]
            return AIMessage(content="", tool_calls=[{
                "name": "apply_training", "id": "saved-behavior",
                "args": {key: record[key] for key in ("unit_id", "teaching_id", "revision")}}])

    async def api(path, payload, _context, *, method):
        assert method == "GET" and payload is None
        assert path == "/api/training?unit_id=" + card["unit_id"]
        return {"catalog": catalog, "records": store.list_records(card["unit_id"], space_id="main")}

    async def execute(name, arguments, step_id):
        assert name == "apply_training"
        prepared = await prepare_training_application(arguments, context, api)
        prepared_calls.append((prepared, step_id))
        return {"result": "accepted"}

    model = ContextModel()
    run = GraphRun("context-workflow", context.session_id, "我想要阅读了" if role == "lamp" else "打个招呼吧",
                   time.time() + 60, agent_id=card["unit_id"], origin_kind="user_delegated")
    result = await run_role_graph(run, model=model, tool_schemas={"apply_training": ApplyTraining},
        build_messages=lambda current: teachings.messages(role, current=current)[0],
        execute=execute)
    assert result.status == "accepted" and model.calls == 1
    prepared, step_id = prepared_calls[0]
    assert step_id == "saved-behavior" and prepared["training_ref"] == saved["teaching_id"]
    assert prepared["training_revision"] == 1 and prepared["training_overrides"] == {}
    assert prepared["record"]["definition"] == saved["definition"]
    if role == "lamp":
        assert prepared["steps"][0]["defaults"] == PURPLE
    else:
        edges = {edge["id"]: edge for edge in catalog["transitions"]}
        assert [edges[step["transition_ids"][0]]["action"]
                for step in prepared["steps"]] == ["StandUp", "Hello"]
    assert store.list_records(card["unit_id"], space_id="main") == [saved]
