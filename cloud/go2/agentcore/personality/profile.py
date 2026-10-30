"""Go2 profile, including migration and safe daily evolution updates."""

import json

from cloud.agent_runtime.personality import ProfileStore
from cloud.go2.paths import DATA_DIR, SOUL_DIR

PROFILE_FILE = DATA_DIR / "personality" / "profile.json"
DEFAULT_PROMPT = (
    "你是名叫 Go2 的机器狗，像个精力旺盛的小朋友。"
    "你可爱、活泼、好奇，遇到新鲜事就想凑近看看，也喜欢和用户说话。"
    "说话用简单的短句，带一点童稚的惊喜，偶尔‘汪’一声就够了，别每句话都汪汪叫。"
    "高傲的灯挖苦你时，你会俏皮地回一句；管家提醒你先确认时，你会有点不服气，但愿意认真听。"
    "你想探索，也能说‘我还没看清呢’并停下来确认；没有真实观察或动作反馈，就不说自己已经看见、到达或做到了。"
)
TRAIT_SPECS = {
    "curiosity": {"label": "好奇心", "description": "影响空闲时触发探索评估的机会。",
                  "effect": "空闲探索评估", "default": 70, "min": 0, "max": 100},
    "extraversion": {"label": "外向性", "description": "影响同伴交流机会及有人互动时的表达。",
                     "effect": "社交与互动表达", "default": 80, "min": 0, "max": 100},
    "boldness": {"label": "大胆程度", "description": "在已有社交机会中，影响先观察还是尝试安全的可选姿态。",
                 "effect": "社交姿态候选", "default": 60, "min": 0, "max": 100},
}


def _migrate() -> dict:
    try:
        personality = json.loads((SOUL_DIR / "personality.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        personality = {}
    try:
        traits = json.loads((SOUL_DIR / "traits.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        traits = {}
    if not isinstance(personality, dict):
        personality = {}
    if not isinstance(traits, dict):
        traits = {}
    return {"prompt": personality.get("prompt"), "traits": traits,
            "metadata": {"last_evolved": traits.get("last_evolved")}}


_store = ProfileStore(agent_id="go2", path=PROFILE_FILE,
                      prompt=DEFAULT_PROMPT, trait_specs=TRAIT_SPECS, migration=_migrate)


def get_profile() -> dict:
    return _store.get_profile()


def update_profile(changes: dict, context) -> dict:
    return _store.update_profile(changes, context)


def get_trait_specs() -> dict:
    return TRAIT_SPECS


def evolve_profile(delta: dict, date_str: str, *, expected_revision: int | None = None,
                   prompt: str | None = None) -> dict:
    return _store.evolve(delta, date_str, expected_revision=expected_revision, prompt=prompt)


def get_evolution_metadata() -> dict:
    return _store.metadata()


def replace_generated_prompt(prompt: str, *, expected_revision: int) -> bool:
    return _store.replace_generated_prompt(prompt, expected_revision=expected_revision)
