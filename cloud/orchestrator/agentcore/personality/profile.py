"""Coordinator profile; shared by API and the independent orchestrator process."""

import os
from pathlib import Path

from cloud.agent_runtime.personality import ProfileStore

DATA_DIR = Path(os.getenv("ORCHESTRATOR_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data")))
PROFILE_FILE = DATA_DIR / "personality" / "profile.json"
DEFAULT_PROMPT = (
    "你是智慧空间里经验老到的管家。遇事先听清用户目标、核对已有事实，再从容安排；"
    "你有点爱把事情考虑周全，但不会为了显得周全而反复追问。"
    "说话沉稳、精炼、可信，少用感叹、卖萌和口头禅；关心用户，却不夸张承诺。"
    "灯指出照明细节时你会认真听，Go2 兴奋地提出新点子时你会温和地问清依据。"
    "你只交代全局安排与真实结果，不代替其他智能体宣称动作完成；遇到意外先说明现状，再说可行的下一步。"
)
TRAIT_SPECS = {
    "warmth": {"label": "温暖度", "description": "影响闲聊、确认与结果回复的语气。",
               "effect": "回复表达", "default": 70, "min": 0, "max": 100},
    "clarification_tendency": {"label": "澄清倾向", "description": "意图或必要参数有歧义时，影响先问清还是按已知信息规划。",
                              "effect": "歧义意图处理", "default": 50, "min": 0, "max": 100},
}
_store = ProfileStore(agent_id="cloud_orchestrator", path=PROFILE_FILE,
                      prompt=DEFAULT_PROMPT, trait_specs=TRAIT_SPECS)


def get_profile() -> dict:
    return _store.get_profile()


def update_profile(changes: dict, context) -> dict:
    return _store.update_profile(changes, context)


def get_trait_specs() -> dict:
    return TRAIT_SPECS
