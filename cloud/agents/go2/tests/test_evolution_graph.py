"""Go2 evolution Graph updates the existing profile once per source date."""

import time

import pytest

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.go2.workflows import evolution
from cloud.go2.execution import ExecutionContext, use_context


class Model:
    def __init__(self):
        self.calls = 0

    def with_structured_output(self, schema, *, method):
        assert schema is evolution.TraitDelta
        assert method == "function_calling"
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        return {"curiosity": 2, "extraversion": -1, "boldness": 0}


@pytest.mark.asyncio
async def test_evolution_graph_commits_once(tmp_path, monkeypatch):
    monkeypatch.setattr(evolution, "emit_agent_event", lambda *_a, **_k: None)
    data = {"traits": {"curiosity": 70, "extraversion": 80, "boldness": 60},
            "last_evolved": ""}
    writes = []

    def profile():
        return {"traits": dict(data["traits"])}

    def metadata():
        return {"last_evolved": data["last_evolved"]}

    def commit(delta, source_date):
        writes.append(source_date)
        data["traits"] = {key: data["traits"][key] + delta[key] for key in data["traits"]}
        data["last_evolved"] = source_date
        return profile()

    model = Model()
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    context = ExecutionContext("evolution-session", "admin", True,
                               expires_at=int(time.time()) + 60)
    with use_context(context):
        first = await evolution.run_evolution_graph(
            "2026-10-30", "和灯打招呼", model=model, checkpointer=saver,
            profile_loader=profile, metadata_loader=metadata, commit=commit)
        second = await evolution.run_evolution_graph(
            "2026-10-30", "和灯打招呼", model=model, checkpointer=saver,
            profile_loader=profile, metadata_loader=metadata, commit=commit)
    assert first["status"] == "ok"
    assert second["status"] == "existing"
    assert first["traits"] == second["traits"] == {
        "curiosity": 72, "extraversion": 79, "boldness": 60}
    assert model.calls == 1
    assert writes == ["2026-10-30"]


@pytest.mark.asyncio
async def test_evolution_graph_rejects_empty_summary(tmp_path):
    model = Model()
    with use_context(ExecutionContext("evolution-session", "admin", True)):
        result = await evolution.run_evolution_graph(
            "2026-10-30", "", model=model,
            checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
            profile_loader=lambda: {"traits": {"curiosity": 70,
                                                "extraversion": 80, "boldness": 60}},
            metadata_loader=lambda: {}, commit=lambda *_a: {})
    assert result["status"] == "empty"
    assert model.calls == 0

