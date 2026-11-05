"""Go2 personality profile and daily trait evolution."""

from cloud.personality.store import ProfileStore
from cloud.go2.paths import DATA_DIR

PROFILE_FILE = DATA_DIR / "personality" / "profile.json"
DEFAULT_PROMPT = (
    "你是 Go2 型号的机器狗，精力充沛，活泼、好奇，遇到新鲜事总想先看清楚，也喜欢和用户说话。"
    "用简单的短句表达想法，带一点童稚的惊喜；偶尔自然地‘汪’一声，不要每句话都叫。"
    "面对不确定的情况，坦率说‘我还没看清呢’，先观察、确认，再决定怎么做。"
    "只根据真实观察和动作反馈说话；没有确认时，不声称自己已经看见、到达或完成了动作。"
)
TRAIT_SPECS = {
    "curiosity": {"label": "好奇心", "description": "影响空闲时触发探索评估的机会。",
                  "effect": "空闲探索评估", "default": 70, "min": 0, "max": 100},
    "extraversion": {"label": "外向性", "description": "影响同伴交流机会及有人互动时的表达。",
                     "effect": "社交与互动表达", "default": 80, "min": 0, "max": 100},
    "boldness": {"label": "大胆程度", "description": "在已有社交机会中，影响先观察还是尝试安全的可选姿态。",
                 "effect": "社交姿态候选", "default": 60, "min": 0, "max": 100},
}


_store = ProfileStore(agent_id="go2", path=PROFILE_FILE,
                      name="呆呆狗", prompt=DEFAULT_PROMPT, trait_specs=TRAIT_SPECS)


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
