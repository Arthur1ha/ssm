"""Go2 conversations feed one daily summary and at most one trait update."""

import json
import time
from datetime import date, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.go2.memory.episode import EpisodeMemory, EventType, read_day
from cloud.agents.go2.memory.recall import recall_memory
from cloud.agents.go2.workflows import daily_maintenance, daily_summary, evolution
from cloud.go2.execution import ExecutionContext, use_context


def test_completed_admin_dialogue_is_private_and_idempotent(tmp_path):
    store = EpisodeMemory(tmp_path / "episodes")
    admin = ExecutionContext("owner", "admin", True)
    visitor = ExecutionContext("guest", "visitor", False)
    assert store.add_dialogue("run-1", "昨天聊过的颜色", "我记得那盏灯", admin)
    assert not store.add_dialogue("run-1", "昨天聊过的颜色", "我记得那盏灯", admin)
    assert not store.add_dialogue("run-2", "访客的秘密", "不会保存", visitor)
    rows = read_day(date.today().isoformat(), store.episodes_dir)
    assert len(rows) == 1
    assert rows[0]["event_type"] == EventType.DIALOGUE.value
    assert "昨天聊过的颜色" in rows[0]["content"]
    assert "访客的秘密" not in rows[0]["content"]


def test_prior_dialogue_can_be_recalled_without_guest_access(tmp_path):
    store = EpisodeMemory(tmp_path / "episodes")
    assert store.add_dialogue("run-1", "上次聊过窗边的花", "我想再看看花", ExecutionContext("owner", "admin", True))
    admin = recall_memory("上次聊过什么", actor="admin", persist=True,
                          session_id="owner", episode_store=store)
    guest = recall_memory("上次聊过什么", actor="visitor", persist=False,
                          session_id="guest", episode_store=store)
    assert "窗边的花" in admin.text
    assert "窗边的花" not in guest.text


def test_old_episode_waits_for_its_summary_before_cleanup(tmp_path):
    old_day = (date.today() - timedelta(days=8)).isoformat()
    episodes = tmp_path / "memory" / "episodes"
    episodes.mkdir(parents=True)
    old_file = episodes / f"{old_day}.jsonl"
    old_file.write_text(json.dumps({"ts": time.time(), "event_type": "OBSERVATION",
                                    "content": "旧观察"}) + "\n", encoding="utf-8")
    EpisodeMemory(episodes)
    assert old_file.exists()
    summaries = episodes.parent / "summaries"
    summaries.mkdir()
    (summaries / f"{old_day}.md").write_text("已概括", encoding="utf-8")
    EpisodeMemory(episodes)
    assert not old_file.exists()


def test_daily_summary_keeps_dialogue_on_noisy_day(tmp_path):
    source_date = "2026-11-04"
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    rows = [{"ts": 1793780000 + i, "event_type": EventType.OBSERVATION.value,
             "content": f"noise-{i}"} for i in range(100)]
    rows[50] = {"ts": 1793780050, "event_type": EventType.DIALOGUE.value,
                "content": "用户：我喜欢安静\nGo2：那我轻声说话"}
    (episodes / f"{source_date}.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    text, count = daily_summary._bounded_episodes(source_date, episodes)
    assert count == 100
    assert "我喜欢安静" in text
    assert len(text) <= daily_summary.memory._SUMMARY_INPUT_CHARS


@pytest.mark.asyncio
async def test_maintenance_summarizes_and_evolves_yesterday_once(monkeypatch):
    summary = {"text": None, "last_evolved": "2026-10-20"}
    calls = []

    def get_summary(day):
        calls.append(("read", day))
        return summary["text"]

    async def generate(day):
        calls.append(("summarize", day))
        summary["text"] = "用户说喜欢安静；Go2 答应轻声回应。"
        return summary["text"]

    async def evolve(day, text, *, model):
        calls.append(("evolve", day, text))
        summary["last_evolved"] = day
        return {"status": "ok"}

    monkeypatch.setattr(daily_maintenance.daily_summary, "get_summary", get_summary)
    monkeypatch.setattr(daily_maintenance.daily_summary, "generate_and_save", generate)
    monkeypatch.setattr(daily_maintenance.profile, "get_evolution_metadata",
                        lambda: {"last_evolved": summary["last_evolved"]})
    monkeypatch.setattr(daily_maintenance, "run_evolution_graph", evolve)
    monkeypatch.setattr(daily_maintenance, "get_text_llm", lambda: object())

    assert await daily_maintenance.maintain_yesterday(date(2026, 11, 5)) == "ok"
    assert await daily_maintenance.maintain_yesterday(date(2026, 11, 5)) == "existing"
    assert [call[0] for call in calls].count("summarize") == 1
    assert [call[0] for call in calls].count("evolve") == 1
    assert ("summarize", "2026-11-04") in calls


@pytest.mark.asyncio
async def test_yesterday_dialogue_reaches_trait_decision(tmp_path, monkeypatch):
    source_date = (date.today() - timedelta(days=1)).isoformat()
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    row = {"ts": datetime.fromisoformat(source_date + "T12:00:00").timestamp(),
           "event_type": EventType.DIALOGUE.value,
           "content": "用户：我喜欢你安静一点\nGo2：好的，我会更轻声地回应", "run_id": "chat-1"}
    (episodes / f"{source_date}.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    class SummaryModel:
        async def ainvoke(self, messages):
            assert "我喜欢你安静一点" in str(messages)
            return AIMessage(content="用户希望我安静一点，我答应轻声回应。")

    class TraitModel:
        def with_structured_output(self, schema, *, method):
            return self

        async def ainvoke(self, messages):
            assert "用户希望我安静一点" in str(messages)
            return {"curiosity": 0, "extraversion": -2, "boldness": 0}

    state = {"traits": {"curiosity": 70, "extraversion": 80, "boldness": 60},
             "last_evolved": ""}

    def commit(delta, day):
        state["traits"]["extraversion"] += delta["extraversion"]
        state["last_evolved"] = day
        return {"traits": dict(state["traits"])}

    monkeypatch.setattr(daily_summary, "get_profile", lambda: {"prompt": "Go2", "name": "呆呆狗"})
    monkeypatch.setattr(daily_summary, "emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr(evolution, "emit_agent_event", lambda *_a, **_k: None)
    with use_context(ExecutionContext("owner", "admin", True, expires_at=int(time.time()) + 60)):
        summarized = await daily_summary.run_daily_summary_graph(
            source_date, model=SummaryModel(), episodes_dir=episodes,
            base_dir=tmp_path / "summaries",
            checkpointer=FileCheckpointSaver(tmp_path / "summary-checkpoints"))
        evolved = await evolution.run_evolution_graph(
            source_date, summarized["summary"], model=TraitModel(),
            checkpointer=FileCheckpointSaver(tmp_path / "evolution-checkpoints"),
            profile_loader=lambda: {"traits": dict(state["traits"])},
            metadata_loader=lambda: {"last_evolved": state["last_evolved"]},
            commit=commit)
    assert summarized["status"] == "ok"
    assert evolved["status"] == "ok"
    assert state["traits"]["extraversion"] == 78
    assert state["last_evolved"] == source_date
