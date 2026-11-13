"""Dynamic Go2 decision prompt, retaining the saved personality and safety rules."""

from __future__ import annotations

from cloud.agents.go2.personality.behavior import reply_persona
from cloud.agents.go2.personality.profile import get_profile
from cloud.personality.names import agent_names
from cloud.agents.common.messages import role_messages
from cloud.agents.common.training.context import DECISION_GUIDANCE, decision_context
from cloud.agents.go2.prompt_contract import _FIXED_RULES
from cloud.go2.connection import go2
from cloud.go2.space_context import prompt_space_context
from cloud.go2.perception.identity import identity_skill


def persona_reply_prompt(run, profile: dict | None = None) -> str:
    """The robot may talk about an invitation even when its body is offline."""
    profile = profile or get_profile()
    return (
        f"{reply_persona(profile)}\n"
        "你以机器狗的第一人称直接接话，按当前人格自然表达，不写任务报告。"
        "身体离线仍可聊天，但不能声称执行了动作。"
        "运动和搜索结论以工具的实测量、停稳确认和受阻原因为准；"
        "保留实际距离或转角，未完成时也说清目标量，不把接口响应当成视觉验证。"
        "当前人物身份只取新鲜的身份识别结果；unknown 称陌生人或其他人，"
        "idle、searching、error、disabled 表示身份未确认，不能从历史记忆或画面描述认定主人。"
    )


def build_messages(run, *, supplement: str = "", memory: str = "", space_id: str = "main"):
    """Read latest profile and live device context before every model decision."""
    profile = get_profile()
    context = {"agent_names": agent_names(), "connected": go2.is_connected,
               "personality_traits": profile["traits"],
               "body_state": getattr(go2, "body_state", "unknown"),
               "device_state": go2.state_snapshot() if hasattr(go2, "state_snapshot") else {},
               "identity": identity_skill.context(),
               "space": prompt_space_context(),
               "training": decision_context("go2", space_id=space_id,
                   origin_kind=run.origin_kind or "user_delegated")}
    if memory:
        context["memory"] = memory
    instructions = (_FIXED_RULES + "\n" + DECISION_GUIDANCE + "\n" + persona_reply_prompt(run, profile))
    return role_messages(instructions, run.goal, context=context, supplement=supplement)
