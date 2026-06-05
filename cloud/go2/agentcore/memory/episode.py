# cloud/go2/episode_memory.py
import time
from collections import deque
from enum import Enum
from typing import TypedDict


class EventType(str, Enum):
    VISION_CHANGE = "VISION_CHANGE"
    ACTION_TAKEN  = "ACTION_TAKEN"
    USER_COMMAND  = "USER_COMMAND"
    OBSERVATION   = "OBSERVATION"


class MemoryEntry(TypedDict):
    ts:         float
    event_type: str
    content:    str


class EpisodeMemory:
    def __init__(self, maxlen: int = 20):
        self._buffer: deque[MemoryEntry] = deque(maxlen=maxlen)

    def add(self, event_type: EventType, content: str) -> None:
        self._buffer.append({
            "ts":         time.time(),
            "event_type": event_type.value,
            "content":    content,
        })

    def entries(self) -> list[MemoryEntry]:
        return list(self._buffer)

    def format_context(self) -> str:
        if not self._buffer:
            return "（暂无近期事件）"
        now = time.time()
        lines = [
            f"[{int(now - e['ts'])}s前] {e['content']}"
            for e in reversed(self._buffer)
        ]
        return "最近事件（最新在前）：\n" + "\n".join(lines)


episode_memory = EpisodeMemory()
