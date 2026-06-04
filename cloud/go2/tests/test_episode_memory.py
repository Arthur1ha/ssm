# cloud/go2/tests/test_episode_memory.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import time


def test_format_context_returns_placeholder_when_empty():
    from cloud.go2.episode_memory import EpisodeMemory
    mem = EpisodeMemory()
    assert mem.format_context() == "（暂无近期事件）"


def test_add_creates_entry_with_correct_fields():
    from cloud.go2.episode_memory import EpisodeMemory, EventType
    mem = EpisodeMemory()
    mem.add(EventType.ACTION_TAKEN, "执行了 Hello")
    entries = mem.entries()
    assert len(entries) == 1
    assert entries[0]["content"] == "执行了 Hello"
    assert entries[0]["event_type"] == "ACTION_TAKEN"
    assert entries[0]["ts"] <= time.time()


def test_maxlen_evicts_oldest_entry():
    from cloud.go2.episode_memory import EpisodeMemory, EventType
    mem = EpisodeMemory(maxlen=2)
    mem.add(EventType.ACTION_TAKEN, "第1条")
    mem.add(EventType.ACTION_TAKEN, "第2条")
    mem.add(EventType.ACTION_TAKEN, "第3条")
    contents = [e["content"] for e in mem.entries()]
    assert contents == ["第2条", "第3条"]


def test_format_context_lists_newest_first():
    from cloud.go2.episode_memory import EpisodeMemory, EventType
    mem = EpisodeMemory()
    mem.add(EventType.VISION_CHANGE, "第一件事")
    mem.add(EventType.ACTION_TAKEN, "第二件事")
    ctx = mem.format_context()
    assert ctx.index("第二件事") < ctx.index("第一件事")


def test_format_context_includes_header():
    from cloud.go2.episode_memory import EpisodeMemory, EventType
    mem = EpisodeMemory()
    mem.add(EventType.VISION_CHANGE, "画面变化")
    assert "最近事件" in mem.format_context()


def test_module_singleton_is_episodememory_instance():
    from cloud.go2.episode_memory import episode_memory, EpisodeMemory
    assert isinstance(episode_memory, EpisodeMemory)
