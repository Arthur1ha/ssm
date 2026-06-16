"""兼容性 wrapper：cloud.go2.episode_memory → cloud.go2.agentcore.memory.episode

供旧代码/测试使用。新代码应直接导入 cloud.go2.agentcore.memory.episode。
"""
from cloud.go2.agentcore.memory.episode import (
    EpisodeMemory,
    EventType,
    MemoryEntry,
    read_day,
    episode_memory,
)

__all__ = [
    "EpisodeMemory",
    "EventType",
    "MemoryEntry",
    "read_day",
    "episode_memory",
]
