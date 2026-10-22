import json
from datetime import date, datetime, timedelta

from cloud.go2.agentcore.memory.episode import EpisodeMemory
from cloud.go2.agentcore.memory.recall import recall_memory


def _write_event(directory, day, hour, event_type, content):
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.combine(day, datetime.min.time()).replace(hour=hour).timestamp()
    with (directory / f"{day.isoformat()}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"ts": stamp, "event_type": event_type, "content": content}, ensure_ascii=False) + "\n")


def test_large_day_is_bounded_and_prefers_relevant_recent_events(tmp_path):
    today = date.today()
    events = tmp_path / "episodes"
    for n in range(80):
        _write_event(events, today, 10, "OBSERVATION", f"普通事件 {n} " + "背景" * 30)
    _write_event(events, today, 11, "ACTION_TAKEN", "在北边走廊巡视")
    result = recall_memory(
        "今天去过北边走廊吗", actor="admin", session_id="admin-1",
        episode_store=EpisodeMemory(events), budget_chars=380, today=today,
    )
    assert len(result.text) <= 380
    assert "北边走廊" in result.text
    assert len(result.fragments) <= 12
    assert "历史数据" in result.text


def test_specific_day_reads_matching_summary_before_other_events(tmp_path):
    today = date.today()
    target = today - timedelta(days=2)
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / f"{target.isoformat()}.md").write_text(
        f"<!-- source_date: {target.isoformat()} -->\n那天去了厨房", encoding="utf-8"
    )
    result = recall_memory(
        f"{target.isoformat()} 发生什么", actor="admin", session_id="admin-1",
        episode_store=EpisodeMemory(tmp_path / "episodes"), summaries_dir=summaries,
        budget_chars=100, today=today,
    )
    assert "那天去了厨房" in result.text
    assert result.sources == ("daily_summary",)
    assert target.isoformat() in result.text


def test_chinese_calendar_date_selects_the_same_summary(tmp_path):
    today = date.today()
    target = today - timedelta(days=2)
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / f"{target.isoformat()}.md").write_text("去了车库", encoding="utf-8")
    result = recall_memory(
        f"{target.month}月{target.day}日去了哪里", actor="admin", session_id="admin",
        episode_store=EpisodeMemory(tmp_path / "episodes"), summaries_dir=summaries,
        today=today,
    )
    assert "车库" in result.text


def test_visitor_sees_only_supplied_session_and_immediate_state(tmp_path):
    today = date.today()
    events = tmp_path / "episodes"
    _write_event(events, today, 10, "USER_COMMAND", "访客 B 的秘密")
    result = recall_memory(
        "今天发生什么", actor="visitor", session_id="visitor-a",
        session_history=[{"role": "user", "content": "访客 A 说去门口"}],
        episode_store=EpisodeMemory(events), current_state="当前电量 80%",
        today=today,
    )
    assert "访客 A" in result.text
    assert "访客 B" not in result.text
    assert "当前电量" in result.text
    assert all(part.source in ("current_state", "session:visitor-a") for part in result.fragments)


def test_action_request_uses_few_recent_events_and_unknown_is_explicit(tmp_path):
    store = EpisodeMemory(tmp_path / "episodes")
    empty = recall_memory(
        "上周发生什么", actor="admin", session_id="admin", episode_store=store,
        summaries_dir=tmp_path / "summaries",
    )
    assert "无相关记忆" in empty.text
    today = date.today()
    for n in range(9):
        _write_event(store._dir, today, 9, "ACTION_TAKEN", f"执行动作 {n}")
    result = recall_memory("站起来", actor="admin", session_id="admin", episode_store=store)
    assert len(result.fragments) <= 3


def test_missing_history_is_unknown_even_with_live_state(tmp_path):
    store = EpisodeMemory(tmp_path / "episodes")
    result = recall_memory(
        "昨天做了什么", actor="admin", session_id="admin", episode_store=store,
        summaries_dir=tmp_path / "summaries", current_state="当前电量 80%",
    )
    assert "当前电量" in result.text
    assert "无相关历史记忆" in result.text
