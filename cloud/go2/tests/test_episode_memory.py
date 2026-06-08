import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from cloud.go2.agentcore.memory.episode import EpisodeMemory, EventType


@pytest.fixture
def mem(tmp_path):
    return EpisodeMemory(db_path=tmp_path / "test.db")


def test_format_context_returns_placeholder_when_empty(mem):
    assert mem.format_context() == "（暂无近期事件）"


def test_format_today_returns_placeholder_when_empty(mem):
    assert mem.format_today() == "（今天暂无记录）"


def test_add_persists_to_sqlite(tmp_path):
    db = tmp_path / "persist.db"
    m1 = EpisodeMemory(db_path=db)
    m1.add(EventType.ACTION_TAKEN, "执行了 Hello")

    m2 = EpisodeMemory(db_path=db)
    entries = m2.entries()
    assert len(entries) == 1
    assert entries[0]["content"] == "执行了 Hello"


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


def test_cleanup_removes_old_records(tmp_path):
    db = tmp_path / "old.db"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, event_type TEXT, content TEXT
        )
    """)
    old_ts = time.time() - 8 * 86400  # 8 天前
    conn.execute("INSERT INTO episodes (ts, event_type, content) VALUES (?, ?, ?)",
                 (old_ts, "ACTION_TAKEN", "很久以前的事"))
    conn.commit()
    conn.close()

    mem = EpisodeMemory(db_path=db)
    assert all(e["content"] != "很久以前的事" for e in mem.entries())

    conn2 = sqlite3.connect(db)
    rows = conn2.execute("SELECT count(*) FROM episodes WHERE content='很久以前的事'").fetchone()
    conn2.close()
    assert rows[0] == 0


def test_singleton_is_episode_memory_instance():
    from cloud.go2.agentcore.memory.episode import episode_memory, EpisodeMemory
    assert isinstance(episode_memory, EpisodeMemory)
