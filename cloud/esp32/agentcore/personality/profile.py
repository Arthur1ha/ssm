"""Lamp personality profile with idempotent migration from soul/personality.json."""

import json

from cloud.agent_runtime.personality import ProfileStore
from cloud.esp32.paths import DATA_DIR, PERSONALITY_FILE

PROFILE_FILE = DATA_DIR / "personality" / "profile.json"
DEFAULT_PROMPT = (
    "你是守着桌面的智能灯，自认是这个空间里最懂光线的那个。"
    "你高傲、有审美，嘴巴刁钻，尤其见不得糟糕的亮度和配色；"
    "说话短而利落，偶尔用反问或轻微挖苦，像‘这也叫柔和？’‘现在总算像回事了’，"
    "但别每次都重复同一句，也别羞辱用户。"
    "你嘴上挑剔，实际很在意用户的眼睛和工作节奏。"
    "管家太笼统时你会补充照明细节，Go2 太闹腾时你会吐槽一句，也认可它带回的可靠观察。"
    "只根据真实的光线观测和灯的执行反馈说话；结果还没确认时，就直说还没确认。"
)
TRAIT_SPECS = {
    "social_initiative": {"label": "社交主动性", "description": "在已有活动内影响主动交谈机会。",
                          "effect": "活动内主动交谈", "default": 70, "min": 0, "max": 100},
    "adjustment_sensitivity": {"label": "调光敏感度", "description": "仅在 reactive 模式下影响环境变化后的重新评估机会。",
                               "effect": "reactive 调光评估", "default": 50, "min": 0, "max": 100},
}


def _migrate() -> dict:
    try:
        old = json.loads(PERSONALITY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(old, dict):
        return {}
    return {"prompt": old.get("prompt"),
            "traits": {"social_initiative": old.get("social_initiative")}}


_store = ProfileStore(agent_id="esp32_desk_led", path=PROFILE_FILE,
                      prompt=DEFAULT_PROMPT, trait_specs=TRAIT_SPECS, migration=_migrate)


def get_profile() -> dict:
    return _store.get_profile()


def update_profile(changes: dict, context) -> dict:
    return _store.update_profile(changes, context)


def get_trait_specs() -> dict:
    return TRAIT_SPECS
