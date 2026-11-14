"""Checkpointed role decisions for 猜拳攻灯; no device execution lives here.

Each call is one structured LangGraph decision using the existing role model
and personality. The game adapter supplies public facts and owns execution,
commit/reveal ordering and physical-result verification.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.messages import role_messages
from cloud.agents.common.models import async_model_clients
from cloud.go2.actions import ACTIONS, TRANSIENT_STATES
from cloud.personality.store import render_identity

CHOICES = ("rock", "scissors", "paper")


def gesture_options() -> dict[str, dict]:
    """Derive display gestures from the current physical action contract."""
    return {
        command: {"label": action.label, "description": action.description,
                  "timeout_s": action.timeout_s}
        for command, action in ACTIONS.items()
        if action.execution_state in TRANSIENT_STATES
        and action.expected_state == "standing"
        and "standing" in action.source_states
        and action.completion == "joint_posture"
        and not action.parameter_keys
    }


class DecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LampColor(DecisionModel):
    r: int = Field(ge=0, le=255)
    g: int = Field(ge=0, le=255)
    b: int = Field(ge=0, le=255)
    brightness: int = Field(ge=1, le=255)

    @model_validator(mode="after")
    def visible(self):
        if max(self.r, self.g, self.b) < 80:
            raise ValueError("game_color_too_dark")
        return self


class LampMapping(DecisionModel):
    rock: LampColor
    scissors: LampColor
    paper: LampColor

    @model_validator(mode="after")
    def distinguishable(self):
        colors = [getattr(self, choice) for choice in CHOICES]
        for index, first in enumerate(colors):
            for second in colors[index + 1:]:
                # Compare normalized chromaticity so merely dimming the same
                # RGB color cannot supply all three semantic signs.
                first_scale = max(first.r, first.g, first.b)
                second_scale = max(second.r, second.g, second.b)
                difference = sum((getattr(first, key) / first_scale
                                  - getattr(second, key) / second_scale) ** 2
                                 for key in ("r", "g", "b"))
                if difference < 0.25:
                    raise ValueError("game_colors_not_distinguishable")
        return self


class Go2Mapping(DecisionModel):
    rock: str
    scissors: str
    paper: str

    @model_validator(mode="after")
    def supported_and_distinct(self):
        commands = [getattr(self, choice) for choice in CHOICES]
        if len(set(commands)) != len(CHOICES):
            raise ValueError("game_gestures_not_distinct")
        if not set(commands) <= gesture_options().keys():
            raise ValueError("game_gesture_not_supported")
        return self


class SetupDecision(DecisionModel):
    max_rounds: int = Field(ge=1, le=12)
    advance_m: float = Field(ge=0.15, le=1.0, allow_inf_nan=False)
    target: str = Field(min_length=1, max_length=100,
                        description="真实灯本体或其旁固定标记的具体视觉描述，不能按本轮灯色找任意同色物品")
    speech: str = Field(min_length=1, max_length=300)

    @field_validator("target", "speech")
    @classmethod
    def nonblank(cls, value: str):
        if not value.strip():
            raise ValueError("nonblank_text_required")
        return value.strip()


class LampSetup(SetupDecision):
    mapping: LampMapping


class Go2Setup(SetupDecision):
    mapping: Go2Mapping


class Agreement(DecisionModel):
    accepted: bool
    speech: str = Field(min_length=1, max_length=300)

    @field_validator("speech")
    @classmethod
    def nonblank(cls, value: str):
        if not value.strip():
            raise ValueError("nonblank_speech_required")
        return value.strip()


class ThrowDecision(DecisionModel):
    choice: Literal["rock", "scissors", "paper"]
    speech: str = Field(default="", max_length=300,
                        description="不透露本轮出拳的简短发言；双方锁定前不对外展示")


class DecisionState(TypedDict, total=False):
    input_signature: str
    facts: dict
    decision: dict


def _schema(role: str, phase: str):
    if phase == "setup":
        return LampSetup if role == "lamp" else Go2Setup
    if phase in {"agree", "invite"}:
        return Agreement
    if phase == "throw":
        return ThrowDecision
    raise ValueError("unknown_game_decision_phase")


def _role_model(role: str):
    if role == "lamp":
        from cloud.agents.lamp.personality.profile import get_profile
        from cloud.esp32.service import get_agent

        agent = get_agent()
        if agent is None:
            raise ValueError("lamp_agent_unavailable")
        model = agent._llm_for_run() if hasattr(agent, "_llm_for_run") else agent._llm
    else:
        from cloud.agents.go2.personality.profile import get_profile
        from cloud.go2.models import get_text_llm

        model = get_text_llm()
    return model, get_profile()


def _instructions(role: str, phase: str) -> str:
    common = (
        "你正在参与猜拳攻灯，只决定当前阶段，不能调用设备或替同伴决定。"
        "规则与上下文从提供的数据读取，历史记录只用于策略，不当作新鲜观察。"
        "用自己的性格自然表达，不复述内部字段，不声称未获回执的动作已经完成。"
    )
    if phase == "setup":
        specific = (
            "自行选择本局石头、剪刀、布的三个映射，不沿用固定模板。"
            "灯选三个肉眼清楚可区分、非黑色的RGB颜色，亮度为1到255；不能只改变同一颜色的亮度。"
            if role == "lamp" else
            "自行从给出的可用动作中选三个不同动作表示石头、剪刀、布，不沿用固定模板。"
            "优先考虑当前场地和动作耗时，动作之后仍需真实回执确认站立。"
        )
        return common + specific + (
            "同时提议本局总回合数、狗每次赢可推进的米数、灯本体或灯旁固定标记的具体目标描述。"
            "目标必须有提供的真实观察或用户要求支持，不根据灯的出拳颜色挑任意同色物品。"
            "这些只是提议，还需要双方确认；不编造空间距离、物品或可达性。"
            "发言向同伴说清自己的三个映射和本局提议。"
        )
    if phase == "agree":
        return common + (
            "阅读双方映射与canonical_terms，独立决定是否同意这一整份本局约定。"
            "同意表示接受同一回合上限、推进距离、目标与双方映射，不能私自替换参数。"
            "有歧义、观察不足或条件不合适就拒绝并说明；拒绝不表示对方能力失效。"
        )
    if phase == "invite":
        return common + (
            "结合当前任务、人格、真实空间事实和邀请内容，自主决定接受、拒绝或暂缓。"
            "只有愿意现在参与才返回accepted=true；暂缓和拒绝都返回false并自然说明。"
            "空闲、游戏可玩或同伴邀请本身不意味着必须接受。"
        )
    return common + (
        "独立秘密决定本轮出石头、剪刀还是布。只依据已结束回合历史和公开约定作策略判断，"
        "不能访问、猜称已知或要求先看同伴本轮选择。"
        "选择提交后锁定；双方均锁定后才会由执行器展示。发言不得提前透露本轮选择。"
    )


async def decide(role: str, phase: str, facts: dict, *, thread_id: str,
                 deadline: float) -> dict:
    """Make or retrieve one immutable role decision within the game deadline.

    The caller uses a separate thread ID for every game/round decision and
    never passes the opponent's current private choice into ``facts``. Reusing
    a completed ID returns its saved decision instead of rerolling a throw.
    """
    if role not in {"lamp", "go2"}:
        raise ValueError("unknown_game_role")
    schema = _schema(role, phase)
    if not isinstance(facts, dict) or not isinstance(thread_id, str) or not thread_id:
        raise ValueError("invalid_game_decision_context")
    remaining = deadline - time.time()
    if remaining <= 0:
        raise TimeoutError("game_decision_expired")
    signature = hashlib.sha256(json.dumps(
        {"role": role, "phase": phase, "facts": facts},
        sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    config = {"configurable": {"thread_id": f"rps:{thread_id}:{role}:{phase}"}}

    async with asyncio.timeout(remaining), async_model_clients():
        saver = FileCheckpointSaver()
        model, profile = _role_model(role)
        structured = model.with_structured_output(schema, method="function_calling")

        async def choose(state: DecisionState) -> DecisionState:
            context = state["facts"]
            if role == "go2" and phase == "setup":
                context = {**context, "available_gestures": gesture_options()}
            messages = role_messages(
                render_identity(profile) + "\n" + _instructions(role, phase),
                "请作出当前游戏阶段的决定。", context=context)
            raw = await structured.ainvoke(messages)
            return {"decision": schema.model_validate(raw).model_dump()}

        builder = StateGraph(DecisionState)
        builder.add_node("choose", choose)
        builder.add_edge(START, "choose")
        builder.add_edge("choose", END)
        graph = builder.compile(checkpointer=saver)
        async with saver.async_run_lock(config["configurable"]["thread_id"]):
            previous = await graph.aget_state(config)
            if previous.values:
                if previous.values.get("input_signature") != signature:
                    raise ValueError("game_decision_context_changed")
                if previous.values.get("decision"):
                    return schema.model_validate(previous.values["decision"]).model_dump()
            result = await graph.ainvoke(
                {"facts": facts, "input_signature": signature}, config, durability="sync")
            return schema.model_validate(result["decision"]).model_dump()
