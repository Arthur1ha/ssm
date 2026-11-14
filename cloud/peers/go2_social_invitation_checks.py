"""Real offline social-to-signed-MQTT invitations retain trusted evidence."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cloud.go2.mode import free_explore
from cloud.mqtt_signing import verify_message
from cloud.peers.messaging import AgentMessenger
from cloud.space.decisions import decision_evidence

LAMP, DOG = "esp32_desk_led", "go2"


@pytest.fixture
def social_bus(monkeypatch, tmp_path):
    monkeypatch.setenv("SSM_MQTT_SIGNING_SECRET", "go2-social-evidence-integration-secret")
    monkeypatch.setenv("SSM_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    now = time.time()
    activity = {"activity_id": "act_social", "context": "你们自己玩一会儿", "status": "active",
                "started_at": now - 1, "devices": {unit: {"status": "joined"} for unit in (LAMP, DOG)}}
    space = {"activity": activity, "constraints": [], "environment": [],
             "peers": [{"unit_id": LAMP, "name": "灯", "online": True, "skills": [],
                        "state_summary": {"status": "current", "as_of": now, "ism": "ON"}}],
             "recent_discoveries": [{"event_id": "evt_toy", "activity_id": "act_social",
                 "unit_id": DOG, "event_type": "observation", "ts": now,
                 "summary": "地上有一个红色玩偶"}], "interaction_history": {}}
    space["decision_evidence"] = decision_evidence(space)
    store = SimpleNamespace(current=lambda: activity, record_event=Mock(return_value={"recorded": True}))
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    monkeypatch.setattr("cloud.peers.social.get_activity_store", lambda: store)
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_a, **_kw: space)
    monkeypatch.setattr("cloud.peers.games.base.game_invitation_ready", lambda *_: True)
    monkeypatch.setattr("cloud.peers.games.registry.game_invitation_ready", lambda *_: True)
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(is_connected=True))
    monkeypatch.setattr(free_explore, "get_profile", lambda: {"prompt": "我是小狗", "traits": {"extraversion": 75}})
    monkeypatch.setattr(free_explore, "prompt_space_context", lambda: "当前空间活动")
    monkeypatch.setattr("cloud.agents.go2.workflows.social.agent_names", lambda: {LAMP: "灯", DOG: "小狗"})
    monkeypatch.setattr("cloud.go2.perception.identity.identity_skill.context", lambda: {})
    sent, received = [], []
    mqtt = SimpleNamespace(publish=lambda topic, body, **_kw: sent.append((topic, body)) or SimpleNamespace(rc=0))
    messenger = AgentMessenger(mqtt, None, None, {LAMP, DOG}, received.append)
    messenger._allowed = lambda *_: True
    # Keep the production _resolve_decision and _decision_context methods: the
    # only injected source is the fresh space snapshot above.
    monkeypatch.setattr("cloud.api.main.get_agent_messenger", lambda: messenger)
    drive = free_explore.Drive()
    drive.owner_session_id = "act_social"
    prompts = []
    refs = ["evt_toy"]

    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, messages):
            prompts.extend(messages)
            return {"decision": "send_message", "target": LAMP,
                    "content": "我刚看到红色玩偶，想玩找颜色！你亮颜色，我去找，愿意吗？",
                    "reason": "我刚看到红色玩偶，想请你给我出道颜色题。",
                    "evidence_refs": refs, "interaction_kind": "game_invite", "game_id": "color_hunt"}

    monkeypatch.setattr(free_explore, "get_text_llm", Model)
    yield SimpleNamespace(drive=drive, bus=messenger, sent=sent, received=received,
                          prompts=prompts, refs=refs, space=space, effects=tmp_path / "effects")
    messenger.close()


def test_drive_social_invitation_reaches_real_resolver_and_signed_bus(social_bus):
    asyncio.run(social_bus.drive._evaluate_social())
    assert len(social_bus.sent) == 1
    topic, body = social_bus.sent[0]
    payload = verify_message(topic, json.loads(body))
    assert (payload["sender"], payload["target"], payload["game_id"]) == (DOG, LAMP, "color_hunt")
    assert payload["decision_basis"] == {
        "reason": "我刚看到红色玩偶，想请你给我出道颜色题。",
        "evidence": [item for item in decision_evidence(social_bus.space) if item["ref"] == "evt_toy"],
    }
    assert "decision_evidence" in social_bus.prompts[1].content
    assert "evt_toy" in social_bus.prompts[1].content
    assert social_bus.drive._social_state.last_sent > 0
    social_bus.bus.handle_message(topic, body)
    social_bus.bus._workers.submit(lambda: None).result()
    assert social_bus.received[0]["decision_basis"] == payload["decision_basis"]


def test_social_model_cannot_invent_observation_reference(social_bus):
    social_bus.refs[:] = ["invented_observation"]
    asyncio.run(social_bus.drive._evaluate_social())
    assert social_bus.sent == []
    records = [json.loads(path.read_text()) for path in social_bus.effects.glob("*.json")]
    assert records[0]["result"] == {"result": "rejected", "error": "decision_evidence_unavailable"}


def test_ordinary_go2_message_does_not_require_game_grounding(social_bus):
    from cloud.go2.tools.tools import send_message
    assert send_message(LAMP, "你好")["result"] == "accepted"
    assert "decision_basis" not in json.loads(social_bus.sent[0][1])


@pytest.mark.parametrize("refs", [[], [False], [""], ["x" * 201], ["evt"] * 9])
def test_go2_tool_schema_rejects_invalid_evidence_arrays(refs):
    from cloud.go2.tools.executor import validate_params
    from cloud.go2.tools.registry import get_tool_spec
    with pytest.raises(ValueError):
        validate_params(get_tool_spec("send_message").params_schema,
                        {"target": LAMP, "content": "想玩吗", "evidence_refs": refs})


def test_go2_tool_schema_and_planner_declaration_share_evidence_fields():
    from cloud.agents.go2.tools.declarations import tool_schemas
    from cloud.go2.tools.executor import validate_params
    from cloud.go2.tools.registry import get_tool_spec
    schema = get_tool_spec("send_message").params_schema
    assert tool_schemas()["send_message"]["parameters"] == schema
    params = {"target": LAMP, "content": "想玩吗", "interaction_kind": "game_invite",
              "game_id": "color_hunt", "decision_reason": "我想玩", "evidence_refs": ["evt_toy"]}
    assert validate_params(schema, params) == params


def test_pending_invitation_defers_new_exploration_but_processes_queued_events(social_bus, monkeypatch):
    from unittest.mock import AsyncMock

    drive = social_bus.drive
    drive._initial_exploration = False
    event = {"event_id": "user_or_accepted_game_event"}
    drive._events.append(event)
    drive._do_explore = AsyncMock()

    async def handle(*_args):
        drive._wake.set()

    drive._handle_event = AsyncMock(side_effect=handle)
    checks = []

    def pending(unit_id):
        checks.append(unit_id)
        drive._handle_event.assert_awaited_once()
        if len(checks) == 2:
            raise asyncio.CancelledError
        drive._wake.set()
        return True

    monkeypatch.setattr("cloud.api.main.get_agent_messenger", lambda: SimpleNamespace(has_pending_game=pending))

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await drive._run_loop()

    asyncio.run(run())
    assert checks == [DOG, DOG]
    drive._do_explore.assert_not_awaited()
