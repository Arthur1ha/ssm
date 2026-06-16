"""兼容性 wrapper：cloud.go2.reactive_mind → cloud.go2.agentcore.skills.reactive

供旧代码/测试使用。新代码应直接导入 cloud.go2.agentcore.skills.reactive。
"""
from cloud.go2.agentcore.skills.reactive import ReactiveMind
from cloud.go2.agentcore.tools.tools import get_text_llm
from cloud.go2.agentcore.memory.episode import episode_memory

__all__ = [
    "ReactiveMind",
    "get_text_llm",
    "episode_memory",
]
