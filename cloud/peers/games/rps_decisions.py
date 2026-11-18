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
from cloud.agents.common.emotions import EmotionModel, EMOTION_INSTRUCTIONS
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


class SetupDecision(EmotionModel, DecisionModel):
    speech: str = Field(default="", max_length=300)

    @field_validator("speech")
    @classmethod
    def nonblank(cls, value: str):
        return value.strip()


class LampColorNames(DecisionModel):
    rock: str = Field(min_length=1, max_length=30)
    scissors: str = Field(min_length=1, max_length=30)
    paper: str = Field(min_length=1, max_length=30)

    @field_validator("rock", "scissors", "paper")
    @classmethod
    def nonblank(cls, value: str):
        if not value.strip():
            raise ValueError("nonblank_color_name_required")
        return value.strip()


class LampSetup(SetupDecision):
    mapping: LampMapping
    color_names: LampColorNames


class LampIdentification(SetupDecision):
    color: LampColor
    speech: str = Field(min_length=1, max_length=300,
                        description="亮灯后供对方辨认的外形和颜色线索")


class Go2Identification(SetupDecision):
    target_visible: bool = Field(description="提供的当前相机观察是否足以辨认灯或灯旁固定标记")
    target: str | None = Field(max_length=100,
        description="观察支持的灯本体或灯旁固定标记描述；看不到或无法辨认时填null，不猜测")

    @model_validator(mode="after")
    def observed_target(self):
        if self.target_visible:
            if not self.target or not self.target.strip():
                raise ValueError("visible_game_target_required")
            self.target = self.target.strip()
        elif self.target is not None:
            raise ValueError("unobserved_game_target")
        return self


class Go2Setup(SetupDecision):
    mapping: Go2Mapping


class PerformanceDecision(EmotionModel, DecisionModel):
    command: str
    speech: str = Field(default="", max_length=300)

    @field_validator("command")
    @classmethod
    def supported(cls, value: str):
        if value not in gesture_options():
            raise ValueError("game_performance_not_supported")
        return value

    @field_validator("speech")
    @classmethod
    def nonblank(cls, value: str):
        return value.strip()


class Agreement(EmotionModel, DecisionModel):
    accepted: bool
    speech: str = Field(default="", max_length=300)

    @field_validator("speech")
    @classmethod
    def nonblank(cls, value: str):
        return value.strip()


class InvitationDecision(Agreement):
    speech: str = Field(min_length=1, max_length=300)

    @field_validator("speech")
    @classmethod
    def response_required(cls, value: str):
        if not value.strip():
            raise ValueError("invitation_response_required")
        return value.strip()


class ThrowDecision(EmotionModel, DecisionModel):
    choice: Literal["rock", "scissors", "paper"]
    speech: str = Field(default="", max_length=300,
                        description="不透露本轮出拳的简短发言；双方锁定前不对外展示")


class DecisionState(TypedDict, total=False):
    input_signature: str
    facts: dict
    decision: dict


def _schema(role: str, phase: str):
    if phase == "identify":
        return LampIdentification if role == "lamp" else Go2Identification
    if phase == "setup":
        return LampSetup if role == "lamp" else Go2Setup
    if phase == "invite":
        return InvitationDecision
    if phase == "agree":
        return Agreement
    if phase == "throw":
        return ThrowDecision
    if phase == "performance" and role == "go2":
        return PerformanceDecision
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
    common = EMOTION_INSTRUCTIONS + (
        "你正在参与猜拳攻灯，只决定当前阶段，不能调用设备或替同伴决定。"
        "规则与上下文从提供的数据读取，历史记录只用于策略，不当作新鲜观察。"
        "用自己的性格自然表达，不复述内部字段，不声称未获回执的动作已经完成。"
        "speech是可选交流，不为每个内部确认或动作生成报幕；"
        "只在有新约定、疑问、发现或实际结果需要告诉对方时发言。"
    )
    if phase == "identify" and role == "go2":
        return common + (
            "双方已经确认映射和规则，现在根据灯亮起后的当前相机观察辨认并定位圆球灯，不修改出拳映射。"
            "用户提供灯是蓝紫色球体、会放在视野内的线索；这是外观与摆放提示，不是已看见的证明。"
            "综合球形、蓝紫色外观线索、发光与识别色判断候选是否唯一。发光可改变外观颜色，"
            "相机呈色也可能与指令RGB不同，不以色调差异单独否定候选，也不靠同色就确认身份。"
            "观察不足、外形证据不足或候选不明确，"
            "返回target_visible=false、target=null并如实说明，不能把同伴自述或历史失败当作当前观察。"
            "确认辨认后返回target_visible=true，target保留当前观察支持的位置、圆球形状或固定标记；"
            "后续出拳会变色，不能把识别色当作永久目标特征。发言说明认灯结果，不声称已出拳。"
        )
    if phase == "identify":
        return common + (
            "用户描述你的实体是蓝紫色球体。双方已确认规则，现在选一个清楚可见的亮灯颜色，帮助狗辨认你。"
            "准备一句亮灯确认后转告狗的提示：用自然语言说你是圆球灯、亮出的颜色，请它在画面中找你；"
            "颜色名称必须与color的RGB一致，不编造自己在房间中的位置。"
            "这只是识别色，不是出拳，不修改已确认的猜拳映射。执行器确认亮灯后才会转告狗。"
        )
    if phase == "setup":
        specific = (
            "自行选择本局石头、剪刀、布的三个映射，不沿用固定模板。"
            "灯选三个肉眼清楚可区分、非黑色的RGB颜色，亮度为1到255；不能只改变同一颜色的亮度。"
            "color_names逐项填写与mapping中RGB相符的自然语言颜色名称，供双方和用户阅读。"
            if role == "lamp" else
            "自行从给出的可用动作中选三个不同动作表示石头、剪刀、布，不沿用固定模板。"
            "优先考虑当前场地和动作耗时，动作之后仍需真实回执确认站立。"
            "现在只提议动作映射；双方确认规则后才认灯，不因尚未看到灯而拒绝制定映射，不猜位置。"
        )
        return common + specific + (
            "本局一把定胜负，平局重来，不协商回合数或推进距离。"
            "这些只是提议，还需要双方确认；不编造空间距离、物品或可达性。"
            "执行器会逐项公布你的结构化映射；speech可简短表达意愿，也可省略，不另造或改写映射。"
            "映射是公开规则，不是本轮出拳；此刻不选拳，不以保密为由隐瞒对应关系。"
        )
    if phase == "agree":
        return common + (
            "阅读双方映射与canonical_terms，独立决定是否同意这一整份本局约定。"
            "核对公开颜色名称与RGB是否相符、动作是否可区分；不一致或含糊就拒绝并说明。"
            "同意表示接受一把定胜负、平局重来与双方映射；狗赢用找物流程接近本局随后确认的灯，"
            "停稳后用Hello挥手抬前腿象征性按灯，等待真实动作完成反馈后关灯，灯赢狗表演一次。"
            "规则确认后才亮识别色并认灯定位，尚未认灯不是拒绝规则的理由；不宣称已经定位或允许移动。"
            "规则有歧义、映射不合适或当前条件不允许参与就拒绝并说明；拒绝不表示对方能力失效。"
        )
    if phase == "invite":
        return common + (
            "结合当前任务、人格、真实空间事实和邀请内容，自主决定接受、拒绝或暂缓。"
            "接受仅表示愿意参与准备；接受后先公开映射并确认规则，再认灯定位，然后秘密选拳并同时展示。"
            "灯尚未亮起、尚未看到灯、尚未选映射或上一局认灯失败，本身不构成拒绝准备的条件。"
            "只有愿意现在参与才返回accepted=true；暂缓和拒绝都返回false并自然说明。"
            "空闲、游戏可玩或同伴邀请本身不意味着必须接受。"
        )
    if phase == "performance":
        return common + (
            "本局灯已赢，轮到你兑现一次表演。结合当前场地、人格和剩余期限，"
            "从available_gestures选择一个支持的动作，可以跳舞或其他合适表演，不发明动作或固定序列。"
            "可简短表达意愿，也可直接选择动作；结果由执行器真实回执确认。"
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
            if role == "go2" and phase in {"setup", "performance"}:
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
