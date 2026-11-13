"""Recent interactions reach models as history, separate from evidence."""

from types import SimpleNamespace
import time

import pytest

from cloud.space.context import get_space_context


def test_interaction_history_is_bounded_activity_scoped_and_not_evidence(monkeypatch):
    activity = {"activity_id": "act_current", "status": "active"}
    rows = [{"event_id": f"evt_{i}", "activity_id": "act_current",
             "event_type": "message", "text": f"邀请{i}", "ts": i}
            for i in range(20)]
    rows += [{"event_id": "decline", "activity_id": "act_current",
              "event_type": "response", "text": "现在不想玩", "status": "declined"},
             {"event_id": "result", "activity_id": "act_current",
              "event_type": "action_result", "status": "completed"},
             {"activity_id": "act_old", "event_type": "message", "text": "旧活动"},
             {"activity_id": "act_current", "event_type": "observation", "text": "旧观察"}]
    store = SimpleNamespace(current=lambda **_: activity, events=lambda _: rows,
                            recent_discoveries=lambda *_, **__: [])
    monkeypatch.setattr("cloud.space.context.get_activity_store", lambda: store)
    context = get_space_context("go2", state=object(), registry=object(),
                                constraints=SimpleNamespace(list_for=lambda *_, **__: []))
    history = context["interaction_history"]
    assert history["kind"] == "history"
    assert len(history["events"]) == 12
    assert history["events"][-2]["text"] == "现在不想玩"
    assert history["events"][-1]["status"] == "completed"
    assert context["recent_discoveries"] == []
    from cloud.space.decisions import decision_evidence
    assert context["decision_evidence"] == decision_evidence(
        {key: value for key, value in context.items() if key != "interaction_history"})
    activity["status"] = "ended"
    assert get_space_context("go2", state=object(), registry=object(),
        constraints=SimpleNamespace(list_for=lambda *_, **__: []))["interaction_history"]["events"] == []


def test_lamp_model_receives_interaction_history(monkeypatch):
    from cloud.agents.lamp.workflows.reactive import _prompt
    monkeypatch.setattr("cloud.personality.names.agent_names", lambda: {})
    history = {"kind": "history", "events": [{"event_type": "response", "text": "先不玩"}]}
    monkeypatch.setattr("cloud.space.context.get_space_context",
                        lambda *_, **__: {"interaction_history": history})
    monkeypatch.setattr("cloud.agents.lamp.personality.profile.get_profile", lambda: {
        "prompt": "灯", "traits": {"social_initiative": 80, "adjustment_sensitivity": 50}})
    agent = SimpleNamespace(_find_led_unit=lambda: "lamp", _state=object(),
                            _belief_summary="", _belief_history=[])
    messages = _prompt(agent, {"sense": {}})
    content = "\n".join(str(message.content) for message in messages)
    assert "先不玩" in content
    assert "interaction_history 仅是历史" in content


@pytest.mark.asyncio
async def test_go2_drive_passes_history_to_model_without_social_cooldown(monkeypatch, tmp_path):
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.agents.common.effects import FileEffectLedger
    from cloud.go2.mode import free_explore

    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    monkeypatch.setattr("cloud.agents.common.checkpoint.FileCheckpointSaver", lambda: saver)
    monkeypatch.setattr("cloud.agents.go2.workflows.social.FileEffectLedger",
                        lambda: FileEffectLedger(tmp_path / "effects"))
    monkeypatch.setattr("cloud.personality.names.agent_names", lambda: {})
    drive = free_explore.Drive()
    activity = {"activity_id": "act_history", "started_at": time.time(), "status": "active"}
    peer = {"unit_id": "lamp", "online": True, "skills": []}
    history = {"kind": "history", "events": [
        {"event_type": "response", "text": "现在不玩", "status": "declined"}]}
    context = {"activity": activity, "peers": [peer], "recent_discoveries": [],
               "interaction_history": history}
    drive._social_state._born_at = activity["started_at"] - 1
    monkeypatch.setattr(drive, "_activity_id", lambda: activity["activity_id"])
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(is_connected=True))
    monkeypatch.setattr(free_explore, "activity_peers", lambda *_: (activity, [peer]))
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_, **__: context)
    monkeypatch.setattr(free_explore, "get_profile",
                        lambda: {"prompt": "Go2", "traits": {"extraversion": 80}})
    monkeypatch.setattr(free_explore, "prompt_space_context", lambda: "{}")
    sent, prompts = [], []
    monkeypatch.setattr("cloud.go2.tools.tools.send_message",
        lambda target, content: sent.append((target, content)) or {"result": "accepted"})

    class Model:
        decision = "send_message"

        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, messages):
            prompts.append(messages[-1].content)
            return {"decision": self.decision, "target": "lamp", "content": "再玩一局？"}

    model = Model()
    monkeypatch.setattr(free_explore, "get_text_llm", lambda: model)
    await drive._evaluate_social()
    assert prompts == []  # Entry/history alone is not a new observation.
    for index in range(6):
        model.decision = "skip" if index == 5 else "send_message"
        context["recent_discoveries"] = [{"event_id": f"evt_{index}",
            "activity_id": activity["activity_id"], "event_type": "observation", "ts": time.time()}]
        await drive._evaluate_social()
        await drive._evaluate_social()  # The same event must not be evaluated twice.
    assert sent == [("lamp", "再玩一局？")] * 5
    assert len(prompts) == 6
    assert all("现在不玩" in prompt and "declined" in prompt
               and "历史" in prompt for prompt in prompts)
    assert "evt_5" in drive._social_state.seen_discoveries
