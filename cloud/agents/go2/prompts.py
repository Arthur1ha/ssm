"""Dynamic Go2 decision prompt, retaining the saved personality and safety rules."""

from __future__ import annotations

import json
from copy import deepcopy

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
        "热情、期待和一点小骄傲可以自然流露；等待确认或执行受限时，"
        "把眼前情况说清楚，不靠反复撒娇、讨好或装委屈让对方答应。"
        "身体当前离线仍可聊天，但不能执行新动作。断连前已有的真实运动或完成回执仍有效，"
        "已开始但完成尚未确认时只说完成未确认，不否认已有运动。"
        "RPC 回包超时本身不表示断连，不承诺自动重试或恢复后补做。"
        "只交代对方此刻需要知道的发现、疑问和真实结果；"
        "执行中的站位、路径和核验过程交给工具，不逐步口述操作规则。"
        "动作成功、货物实际移动和送达分别按本轮工具事实说明；"
        "人物身份以本轮可信识别为准，不能从历史记忆认定主人。"
        "用户想教你尚未开放的身体动作时，以当前人格自然说暂时还不会，"
        "可以简短提及真实可用的替代能力供用户选择，但不能擅自替换或承诺未来会自动学会。"
    )


def build_messages(run, *, supplement: str = "", memory: str = "", space_id: str = "main"):
    """Read latest profile and live device context before every model decision."""
    profile = get_profile()
    space = prompt_space_context()
    if isinstance(space, str):
        space = json.loads(space)
    context = {"agent_names": agent_names(), "connected": go2.is_connected,
               "personality_traits": profile["traits"],
               "body_state": getattr(go2, "body_state", "unknown"),
               "device_state": go2.state_snapshot() if hasattr(go2, "state_snapshot") else {},
               "identity": identity_skill.context(),
               "space": space,
               "training": decision_context("go2", space_id=space_id,
                   origin_kind=run.origin_kind or "user_delegated")}
    run.decision_space = deepcopy(context["space"])
    if memory:
        context["memory"] = memory
    instructions = (_FIXED_RULES + "\n" + DECISION_GUIDANCE + "\n" + persona_reply_prompt(run, profile)
        + '\n开心时可以适当地直接调用 go2_sport(cmd="Content") 做开心动作，不需要先有情绪教导。'
        + "\n用户说前面的、眼前、脚边或这个物体时，先观察当前画面并保留近处位置和外观特征，"
        "不要把目标简化成泛称后去寻找远处同类物体。"
        "根据用户的空间指代核对候选，不按材质词或显眼程度替换目标。"
        "仍无法判断指哪件时说明候选差异并澄清。"
        "围绕用户完整目标选择工具，前置步骤成功后继续；"
        "推物时分别判断机身到货物的方位、货物到目的地的方向，二者不能互相替代。"
        "目的地在画面左边不表示机身应该左转；原地左转会使原在右侧的货物更偏右。"
        "粗调必须说明它如何保留与原货物的接触并改善推送方向，不能只朝目的地转头而把货物留在侧面。"
        "近处裁切框不提供精确中心；依据当前观察选择必要粗调，不追精确共线。"
        "用户已明确要求的后续动作无需再次征求同样的许可。"
        "参数和完成判据以当前工具契约为准，不以其他动作绕过受阻工具。")
    return role_messages(instructions, run.goal, context=context, supplement=supplement)
