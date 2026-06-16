# cloud/go2/tests/test_episode_memory.py
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from cloud.go2.agentcore.memory.episode import EpisodeMemory, EventType, read_day


@pytest.fixture
def mem(tmp_path):
    return EpisodeMemory(episodes_dir=tmp_path)


def test_format_context_returns_placeholder_when_empty(mem):
    assert mem.format_context() == "（暂无近期事件）"


def test_format_today_returns_placeholder_when_empty(mem):
    assert mem.format_today() == "（今天暂无记录）"


def test_add_persists_to_dated_jsonl(tmp_path):
    m1 = EpisodeMemory(episodes_dir=tmp_path)
    m1.add(EventType.ACTION_TAKEN, "执行了 Hello")

    today = datetime.now().strftime("%Y-%m-%d")
    f = tmp_path / f"{today}.jsonl"
    assert f.exists()
    line = f.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["content"] == "执行了 Hello"
    assert rec["event_type"] == "ACTION_TAKEN"


def test_add_reloads_into_buffer_on_restart(tmp_path):
    m1 = EpisodeMemory(episodes_dir=tmp_path)
    m1.add(EventType.ACTION_TAKEN, "执行了 Hello")

    m2 = EpisodeMemory(episodes_dir=tmp_path)
    entries = m2.entries()
    assert any(e["content"] == "执行了 Hello" for e in entries)


def test_add_creates_entry_with_correct_fields(mem):
    mem.add(EventType.ACTION_TAKEN, "执行了 Hello")
    entries = mem.entries()
    assert entries[0]["event_type"] == "ACTION_TAKEN"
    assert entries[0]["content"] == "执行了 Hello"
    assert entries[0]["ts"] <= time.time()


def test_format_today_includes_todays_records(mem):
    mem.add(EventType.OBSERVATION, "探索了北边走廊")
    result = mem.format_today()
    assert "探索了北边走廊" in result
    assert "今天的事件记录" in result


def test_format_context_lists_newest_first(mem):
    mem.add(EventType.VISION_CHANGE, "第一件事")
    mem.add(EventType.ACTION_TAKEN, "第二件事")
    ctx = mem.format_context()
    assert ctx.index("第二件事") < ctx.index("第一件事")


def test_format_context_uses_human_readable_time(mem):
    mem.add(EventType.VISION_CHANGE, "画面变化")
    ctx = mem.format_context()
    assert "秒前" in ctx or "分钟前" in ctx or "今天" in ctx


def test_read_day_returns_entries(tmp_path):
    mem = EpisodeMemory(episodes_dir=tmp_path)
    mem.add(EventType.OBSERVATION, "今天的事件")
    today = datetime.now().strftime("%Y-%m-%d")
    rows = read_day(today, episodes_dir=tmp_path)
    assert len(rows) == 1
    assert rows[0]["content"] == "今天的事件"


def test_read_day_missing_file_returns_empty(tmp_path):
    assert read_day("2026-01-01", episodes_dir=tmp_path) == []


def test_cleanup_removes_old_day_files(tmp_path):
    old_date = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
    old_file = tmp_path / f"{old_date}.jsonl"
    old_file.write_text(
        json.dumps({"ts": time.time() - 8 * 86400,
                    "event_type": "ACTION_TAKEN", "content": "很久以前的事"}) + "\n",
        encoding="utf-8",
    )
    EpisodeMemory(episodes_dir=tmp_path)
    assert not old_file.exists()


def test_event_type_has_agent_response():
    assert EventType.AGENT_RESPONSE.value == "AGENT_RESPONSE"


def test_singleton_is_episode_memory_instance():
    from cloud.go2.agentcore.memory.episode import episode_memory, EpisodeMemory
    assert isinstance(episode_memory, EpisodeMemory)
