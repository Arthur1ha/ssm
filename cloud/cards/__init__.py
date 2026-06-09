"""cloud.cards — 智能体 Agent Card 共享模块（schema、builder、registry）。"""

from cloud.cards.schema import AgentCard, SkillDef, SkillInvoke, Transport
from cloud.cards.builder import build_card_from_manifest, parse_card
from cloud.cards.registry import CardRegistry, get_registry

__all__ = [
    "AgentCard",
    "SkillDef",
    "SkillInvoke",
    "Transport",
    "build_card_from_manifest",
    "parse_card",
    "CardRegistry",
    "get_registry",
]
