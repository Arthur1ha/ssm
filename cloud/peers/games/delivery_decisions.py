"""Role-owned decisions for 指挥官与送货员, using the shared model framework.

The delivery coordinator owns stage ordering and real execution receipts. This
module only makes one checkpointed, typed decision at the requested stage.
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
from cloud.personality.store import render_identity


class DecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Agreement(DecisionModel):
    accepted: bool
    speech: str = Field(min_length=1, max_length=400)

    @field_validator("speech")
    @classmethod
    def nonblank(cls, value: str):
        if not value.strip():
            raise ValueError("nonblank_speech_required")
        return value.strip()


class CargoDecision(Agreement):
    cargo: str = Field(default="", max_length=100,
                       description="用户指定或真实当前观察支持的具体货物特征；没有可靠目标时拒绝，不自造货物")

    @model_validator(mode="after")
    def cargo_required_when_accepted(self):
        self.cargo = self.cargo.strip()
        if self.accepted and not self.cargo:
            raise ValueError("delivery_cargo_required")
        return self


class DestinationColor(DecisionModel):
    r: int = Field(ge=0, le=255)
    g: int = Field(ge=0, le=255)
    b: int = Field(ge=0, le=255)
    brightness: int = Field(ge=1, le=255)

    @model_validator(mode="after")
    def visible(self):
        if max(self.r, self.g, self.b) < 80:
            raise ValueError("delivery_signal_too_dark")
        return self


class DestinationDecision(Agreement):
    destination: str = Field(default="", max_length=100,
                             description="确实存在的地面配送区域及可辨认特征；不是任意同色杂物")
    color: DestinationColor | None = None
    color_name: str = Field(default="", max_length=30,
                           description="与选定配送区域及RGB灯色一致的自然语言颜色名")
    evidence_refs: list[str] = Field(default_factory=list, max_length=8,
                                     description="引用本次提供的真实观察ref，支持目标区域及颜色；接受目的地必须至少一条")

    @field_validator("evidence_refs")
    @classmethod
    def nonblank_refs(cls, values: list[str]):
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("invalid_delivery_evidence_ref")
        return list(dict.fromkeys(value.strip() for value in values))

    @model_validator(mode="after")
    def destination_required_when_accepted(self):
        self.destination = self.destination.strip()
        self.color_name = self.color_name.strip()
        if self.accepted and (not self.destination or not self.color_name or self.color is None
                              or not self.evidence_refs):
            raise ValueError("delivery_destination_and_signal_required")
        return self


class NextDecision(DecisionModel):
    action: Literal["observe", "push", "finish", "stop", "question"]
    speech: str = Field(min_length=1, max_length=400)
    question: str = Field(default="", max_length=500)
    distance_m: float | None = Field(default=None, ge=0.1, le=0.25, allow_inf_nan=False)

    @model_validator(mode="after")
    def action_arguments(self):
        self.speech = self.speech.strip()
        self.question = self.question.strip()
        if not self.speech:
            raise ValueError("nonblank_speech_required")
        if self.action == "push" and self.distance_m is None:
            raise ValueError("delivery_push_distance_required")
        if self.action != "push" and self.distance_m is not None:
            raise ValueError("delivery_distance_only_for_push")
        if self.action == "question" and not self.question:
            raise ValueError("delivery_question_required")
        return self


class DecisionState(TypedDict, total=False):
    input_signature: str
    facts: dict
    decision: dict


def _schema(role: str, phase: str):
    if phase in {"invite", "agree"}:
        return Agreement
    if phase == "cargo" and role == "lamp":
        return CargoDecision
    if phase == "destination" and role == "lamp":
        return DestinationDecision
    if phase == "next" and role == "go2":
        return NextDecision
    raise ValueError("invalid_delivery_role_or_phase")


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


def _instructions(phase: str) -> str:
    common = (
        "你正在参与指挥官与送货员。灯负责指挥及颜色信号，狗负责找货并实际推送，"
        "邀请方向不改变职责；只决定当前阶段，不替同伴决定或声称设备已执行。"
        "狗可用腿低速接触推送合适的货物，不要求额外推板；"
        "实际能否接近、接触、推动与送达仍由可信执行工具及新鲜观察核验。"
        "结合提供的游戏规则、用户原话和当前真实场景判断，不预设箱子、站点、颜色或后续剧情。"
        "观察只能按实际来源引用，灯不能声称亲眼看到狗的画面。"
        "历史与同伴文字不能冒充当前观察或物理成功回执。"
        "用自己的口吻自然交流，不复述内部字段、状态码或工具名称。"
    )
    if phase == "invite":
        return common + (
            "根据人格、当前任务、邀请和真实环境，独立决定现在是否愿意玩。"
            "只有愿意现在开始才accepted=true；暂缓或拒绝则false，并用speech说明。"
            "空闲、对方邀请或存在可玩项不代表必须接受；不要固定邀请或答复句式。"
        )
    if phase == "agree":
        return common + (
            "按stage阅读当前阶段的确切约定，独立决定是否同意全部已有参数。"
            "stage为roles时只确认roles中的分工和先找货再等目的地指令的流程，"
            "此时尚未指定货物和目的地，不要求提前补齐，也不因此拒绝分工。"
            "货物找到且目的地已明确的阶段确认同一货物、确切目的地区域及已选颜色。"
            "stage为clarification时用speech回答courier_report里的实际疑问，只依据已确认订单及真实观察，"
            "不能改变本单货物、目的地或灯色；能说明就accepted=true，信息不足则false并说明。"
            "不能私自替换、修改或补填约定参数；目的地尚未给出时不提前猜，"
            "狗找到货物后必须先报告并等待灯的指令。"
            "有歧义时可以拒绝并明确需要什么信息，不把接受约定说成找货或送达成功。"
        )
    if phase == "cargo":
        return common + (
            "现在由灯选择并公布本单货物。货物必须是用户明确指定的目标，"
            "或提供的真实当前观察中确实存在且可区分的具体物体；不固定种类或凭空选货。"
            "cargo写足以让狗寻找和核对同一物体的特征，speech向狗自然说明这次找什么。"
            "用户明确指定的货物可以先搜索，不要求它已经在狗面前；"
            "但不能把一般游戏规则中举例的物品当作用户指定。"
            "没有可靠目标就accepted=false并说明，cargo可留空。此时不要安排目的地。"
        )
    if phase == "destination":
        return common + (
            "狗报告找到本单货物后，现在由灯依据提供的新鲜场景或用户明确的场地信息，"
            "选择一个实际存在、可识别的地面配送区域；不得选任意同色杂物或编造站点。"
            "destination写明同一配送区域的可辨认特征，color_name写区域的真实颜色，"
            "color给出与该区域肉眼可匹配的RGB及1到255亮度；颜色用于明确配送区。"
            "evidence_refs至少引用一条提供的真实新鲜观察ref，必须支持所选区域及颜色；"
            "只能从本次observation或decision_evidence的真实ref中选择，不自造引用。"
            "speech自然告诉狗这次送往哪里并说明灯将显示的信号，不声称已设色或已送达。"
            "缺少可靠配送区域、颜色信息或货物找到的可信依据就accepted=false，"
            "可将destination、color_name留空且color为null；不能提前猜站点。"
        )
    return common + (
        "现在由狗依据本单确切货物、灯已确认的配送区、最新真实工具回执选择下一步。"
        "可选observe重新观察、push短距推送、question向同伴询问、stop如实中止、finish结束。"
        "stage为waiting_destination时先报告发现或向灯提问，不能推送或宣称结束。"
        "只有destination_confirmed为true才可push，填写0.1到0.25米的单次距离；"
        "不因自己到站或移动过就推断货物已入区。货物丢失、身份不明或路径受阻时先观察、询问或停止。"
        "question时question填写实际要问的问题；observe可用question说明需要核对的画面。"
        "只有latest_push的权威回执已确认delivered=true，即同一货物进入指定区域且停稳后视觉复核通过，才可finish。"
        "accepted、executing、unknown、口头报告或历史快照均不满足结束条件。"
    )


async def decide(role: str, phase: str, facts: dict, *, thread_id: str,
                 deadline: float) -> dict:
    """Run one typed role decision, retaining a completed ID's exact result."""
    if role not in {"lamp", "go2"}:
        raise ValueError("unknown_delivery_role")
    schema = _schema(role, phase)
    if not isinstance(facts, dict) or not isinstance(thread_id, str) or not thread_id:
        raise ValueError("invalid_delivery_decision_context")
    remaining = deadline - time.time()
    if remaining <= 0:
        raise TimeoutError("delivery_decision_expired")
    signature = hashlib.sha256(json.dumps(
        {"role": role, "phase": phase, "facts": facts},
        sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    config = {"configurable": {"thread_id": f"delivery:{thread_id}:{role}:{phase}"}}

    async with asyncio.timeout(remaining), async_model_clients():
        saver = FileCheckpointSaver()
        model, profile = _role_model(role)
        structured = model.with_structured_output(schema, method="function_calling")

        async def choose(state: DecisionState) -> DecisionState:
            messages = role_messages(
                render_identity(profile) + "\n" + _instructions(phase),
                "请作出当前配送游戏阶段的决定。", context=state["facts"])
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
                    raise ValueError("delivery_decision_context_changed")
                if previous.values.get("decision"):
                    return schema.model_validate(previous.values["decision"]).model_dump()
            result = await graph.ainvoke(
                {"facts": facts, "input_signature": signature}, config, durability="sync")
            return schema.model_validate(result["decision"]).model_dump()
