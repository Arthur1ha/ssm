"""Dynamic Go2 decision prompt, retaining the saved personality and safety rules."""

from __future__ import annotations

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
        "身体当前离线仍可聊天，但不能执行新动作。断连前已有的真实运动或完成回执仍有效，"
        "已开始但完成尚未确认时只说完成未确认，不否认已有运动。"
        "RPC 回包超时本身不表示断连，不承诺自动重试或恢复后补做。"
        "运动和搜索结论以工具的实测量、停稳确认和受阻原因为准；"
        "保留实际距离或转角，未完成时也说清目标量，不把接口响应当成视觉验证。"
        "推物能力以go2_push_cargo的真实回执为准：它是受控步态接触尝试，实机效果尚未保证，"
        "不能因没有手、嘴或机械臂就否认已有工具，也不能编造独立踢腿、抓取或可靠顶推能力。"
        "没有执行运动时不能说已经推过；approached只表示靠近，pushed才表示测得货物位移。"
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
    run.decision_space = deepcopy(context["space"])
    if memory:
        context["memory"] = memory
    instructions = (_FIXED_RULES + "\n" + DECISION_GUIDANCE + "\n" + persona_reply_prompt(run, profile)
        + "\n用户说眼前、脚边或这个物体时，先观察当前画面并保留近处位置和外观特征，"
        "不要把目标简化成泛称后去寻找远处同类物体。"
        "找物或导航只是复合任务的前置步骤时，明确final=false，确认成功后继续原目标；"
        "用户已要求推物，无需仅因到附近就再次询问是否尝试。"
        "单独测试能否推动时，可调用go2_push_cargo只传cargo和distance_m，不需要配送区域；"
        "这只核验一次受控前进及货物是否移动，不能声称到达指定位置。"
        "用户要求推到指定区域时仍须传destination，不可省略目标冒充完成；"
        "目的地可以是小色块、地面标记或物体标识，送到大概附近即可，无需货物完全覆盖标记，"
        "也不要求完整区域边界或精确对位；delivered=true表示停稳后的大致到位复核通过。"
        "配送游戏沿用同一货物、目的地和颜色约束。"
        "接触通路、货物雷达测量或对齐不足时，如实说明当前阻塞，"
        "不能用go2_move盲走、表演动作或重复同样请求绕开推送工具的限制。")
    return role_messages(instructions, run.goal, context=context, supplement=supplement)
