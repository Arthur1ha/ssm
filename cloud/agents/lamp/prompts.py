"""Dynamic lamp identity and decision scope; tool contracts live in schemas."""

from __future__ import annotations

import json

from cloud.agents.lamp.personality.profile import get_profile
from cloud.personality.store import render_identity
from cloud.personality.names import agent_names
from cloud.agents.common.messages import role_messages
from cloud.agents.common.training.context import DECISION_GUIDANCE, decision_context
from cloud.space.context import get_space_context


def planning_prompt() -> str:
    """Role instructions; request and live data use separate messages."""
    return (
        "完成本次照明目标即结束。单次调整以执行前的灯光为基准，"
        "成功后不把原请求当成新的反馈反复调节；明确要求多个先后动作时才逐步执行。"
        "教学只引用当前真实灯光；用户已明确的适用情境无需再增加触发方式或限制。"
        "同伴互动由自己决定是否参与，玩法以当前可玩列表为准。"
        "是否邀请须依据当前活动、用户要求和同伴状态，不因进入活动或有可玩项就必定邀请。"
        "游戏邀请和出题设色的 decision_reason 会合并到现有对话或动作内容中展示给用户。"
        "用自己的性格、第一人称说清我为什么现在作这个决定，不写‘选择理由’或执行报告，"
        "并用 evidence_refs 引用 space.decision_evidence 的 ref；"
        "同伴观察要说成它分享的发现，不编造自己的视觉或同伴完整任务情况。"
        "缺少事实时先询问或观察，不补造同伴的发现或意愿。"
    )


def decision_messages(run, *, agent, unit_id: str, supplement: str = "",
                      space_id: str = "main"):
    """Build the current Graph runner's per-turn, freshly loaded context."""
    from cloud.peers.games import playable_games

    space = get_space_context(unit_id, state=agent._state, include_self=False)
    games = [{"target": peer["unit_id"], **game}
             for peer in space["peers"]
             for game in playable_games((space.get("activity") or {}).get("activity_id"),
                                        unit_id, peer["unit_id"])]
    profile = get_profile()
    instructions = planning_prompt() + "\n" + DECISION_GUIDANCE + "\n" + persona_reply_prompt(run, profile)
    return role_messages(instructions, run.goal, supplement=supplement,
                         context={"lamp": agent._state.actuator_snapshot(), "space": space,
                                  "agent_names": agent_names(), "playable_games": games,
                                  "training": decision_context(unit_id, space_id=space_id,
                                      origin_kind=run.origin_kind or "user_delegated")})


def persona_reply_prompt(run, profile: dict | None = None) -> str:
    """Let the lamp's saved identity judge its own observed light and actions."""
    profile = profile or get_profile()
    return (
        f"{render_identity(profile)}\n当前特质：{json.dumps(profile['traits'], ensure_ascii=False)}。"
        "你就是智能灯，以第一人称接话，按人格自然表达看法，不写执行报告或机械重复口头禅。"
        "无需把所有话题都绕回照明。除非用户询问数值，用可感知的灯光效果描述调整，"
        "不罗列 RGB、亮度或色温参数。设置生效不等于已验证用户感受。"
        "本轮执行受限时，区分已生效和未执行的部分，不把运行限制说成设备能力不足。"
    )
