"""Dynamic steward prompt, refreshed before every model turn."""

import hashlib
from cloud.agents.common.messages import role_messages
from cloud.agents.common.runner import GraphRun as Run
from cloud.observability import emit_agent_event
from cloud.agents.steward.personality.profile import get_profile
from cloud.agents.steward.personality.behavior import clarification_guidance, current_persona
from cloud.agents.steward.tools.state import steward_space_context as get_space_context
from cloud.orchestrator import tools as transport
from cloud.personality.names import agent_names


def reply_prompt(run: Run, profile: dict) -> str:
    """The steward speaks for the plan, leaving device voices to devices."""
    return (
        f"{current_persona(profile)}\n"
        "以管家身份回应用户目标和已确认的全局结果。执行请求的最终回复限定一至两句："
        "只说目标是否满足，以及仍未确认或未完成的部分。设备会分别反馈自己的结果，"
        "不复述设备的话、不回顾执行过程、不追加点评、建议或追问。"
        "这项简短收尾要求不限制用户明确请求的解释、分析或普通问答。"
        "只回应本轮用户目标及本轮真实结果。历史对话仅用于解析指代和偏好；"
        "简单控制完成时通常一句确认即可，用可感知的效果描述结果；"
    )


def build_messages(current: Run, supplemental: str = ""):
    profile = get_profile()
    persona = current_persona(profile)
    clarification = clarification_guidance(profile["traits"]["clarification_tendency"])
    emit_agent_event("persona.loaded", trace_id=current.trace_id or current.run_id,
                     run_id=current.run_id, agent_id="cloud_orchestrator",
                     source="profile", role="orchestrator",
                     profile_revision=profile["revision"],
                     content_hash=hashlib.sha256(persona.encode()).hexdigest(),
                     prompt_trimmed=False)
    data = {"agent_names_by_id": agent_names(),
            "space": get_space_context(state=transport._state, registry=transport._registry)}
    prompt = (
        f"澄清偏好：{clarification}\n"
        "你负责理解用户目标并协调当前空间的智能体；只用设备卡公开的技能，"
        "不构造传输地址或权限。临时动作、长期教导、空间要求和自主活动按用户本意区分。"
        "用户表达当前情境时，语义选择已有教导；不据此开启自主活动或修改全局默认。"
        "自主活动中的具体行为和游戏由设备自己决定；需要转达时保留用户指定的发起方和受邀方。"
    )
    return role_messages(prompt + "\n" + reply_prompt(current, profile), current.goal,
                         context=data, supplement=supplemental)
