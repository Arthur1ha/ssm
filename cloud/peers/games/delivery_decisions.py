"""Delivery stage contracts supplied to each device's existing role Graph."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud.agents.common.emotions import EmotionModel, EMOTION_INSTRUCTIONS


class DecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PublicDecision(EmotionModel, DecisionModel):
    decision_reason: str = Field(default="", max_length=300,
                                 description="可选公开选择依据，只进过程详情，不作为发言")
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)


class Agreement(PublicDecision):
    accepted: bool
    speech: str = Field(default="", max_length=400,
                        description="有新的约定、疑问或必要回应时才发言；内部确认可省略")

    @field_validator("speech")
    @classmethod
    def nonblank(cls, value: str):
        return value.strip()


class InvitationDecision(Agreement):
    speech: str = Field(min_length=1, max_length=400)

    @field_validator("speech")
    @classmethod
    def invitation_response(cls, value: str):
        if not value.strip():
            raise ValueError("invitation_response_required")
        return value.strip()


class CargoDecision(InvitationDecision):
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


class DestinationDecision(InvitationDecision):
    destination: str = Field(default="", max_length=100,
                             description="确实存在的配送色块、小地面标记或物体标识及可辨认特征")
    color: DestinationColor | None = None
    color_name: str = Field(default="", max_length=30,
                           description="本单约定的RGB灯色名称；可以是目的地颜色或明确约定的指令色")
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


class NextDecision(PublicDecision):
    action: Literal["observe", "push", "finish", "stop", "question"]
    speech: str = Field(default="", max_length=400,
                        description="可选自然交流；观察和执行动作无需逐步报幕，问题只填question")
    question: str = Field(default="", max_length=500)
    distance_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def action_arguments(self):
        self.speech = self.speech.strip()
        self.question = self.question.strip()
        if self.action != "push" and self.distance_m is not None:
            raise ValueError("delivery_distance_only_for_push")
        if self.action == "question" and not self.question:
            raise ValueError("delivery_question_required")
        return self


class CompletionDecision(NextDecision):
    speech: str = Field(min_length=1, max_length=400,
                        description="依据本单真实结果给出一次最终回应")

    @field_validator("speech")
    @classmethod
    def completion_response(cls, value: str):
        if not value.strip():
            raise ValueError("completion_response_required")
        return value.strip()


def _schema(role: str, phase: str, stage: str = ""):
    if phase == "invite":
        return InvitationDecision
    if phase == "agree":
        return Agreement
    if phase == "cargo" and role == "lamp":
        return CargoDecision
    if phase == "destination" and role == "lamp":
        return DestinationDecision
    if phase == "next" and role == "go2":
        return CompletionDecision if stage in {"delivered", "directional_push_completed"} else NextDecision
    raise ValueError("invalid_delivery_role_or_phase")


def _instructions(phase: str) -> str:
    common = EMOTION_INSTRUCTIONS + (
        "你正在参与指挥官与送货员。灯负责指挥及颜色信号，狗负责找货并实际推送，"
        "邀请方向不改变职责；只决定当前阶段，不替同伴决定或声称设备已执行。"
        "结合提供的游戏规则、用户原话和当前真实场景判断，不预设箱子、站点、颜色或后续剧情。"
        "观察只能按实际来源引用，灯不能声称亲眼看到狗的画面。"
        "历史与同伴文字不能冒充当前观察或物理成功回执。"
        "用自己的口吻自然交流，不复述内部字段、状态码或工具名称。"
        "speech只说对方此刻需要的约定、问题、新发现、受阻或最终结果；"
        "已有信息没有变化时可省略，不复述站位、路径或核验流程，不为每个决策报幕。"
        "decision_reason与evidence_refs仅供过程详情，不复制到speech。"
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
            "现在由灯作为指挥官直接选择并公布本单货物，不向狗询问该选哪个，也不要求狗表态同意选货。"
            "货物必须是用户明确指定的目标，"
            "或提供的真实当前观察中确实存在且可区分的具体物体；不固定种类或凭空选货。"
            "用户未指定货物时，综合当前观察优先选择离狗较近、容易接近、看起来方便推动，"
            "且有一定体积、可辨认厚度或高度的独立物品。多个候选由灯自行权衡决定，"
            "不设固定距离、尺寸门槛或物品种类；不把颜色鲜明当作选货依据。"
            "区分货物与站点标记，地面色块、贴纸及用于标识目的地的小标记留作目的地，"
            "不能仅因它们可见或有颜色就选作货物；物品本身有颜色不影响它成为货物。"
            "只按观察中的相对远近、外形和通路线索判断，不编造精确距离、重量或已能推动的结论。"
            "cargo写足以让狗寻找和核对同一物体的特征，speech向狗自然说明这次找什么。"
            "用户明确指定的货物可以先搜索，不要求它已经在狗面前；"
            "但不能把一般游戏规则中举例的物品当作用户指定。"
            "没有可靠目标就accepted=false并说明，cargo可留空。此时不要安排目的地。"
        )
    if phase == "destination":
        return common + (
            "狗报告找到本单货物后，现在由灯依据提供的新鲜场景或用户明确的场地信息，"
            "选择一个实际存在、可识别的配送目的地，可以是色块、小地面标记或物体标识；不得编造站点。"
            "destination写明同一目的地的可辨认特征，不要求完整边界或面积足够容纳货物。"
            "color_name与color给出本单约定的指令灯色及1到255亮度；可沿用目标颜色，"
            "物体标识没有合适颜色时明确约定一个指令色，不能把指令色冒充观察到的物体颜色。"
            "evidence_refs至少引用一条提供的真实新鲜观察ref，必须支持所选目的地；"
            "只能从本次observation或decision_evidence的真实ref中选择，不自造引用。"
            "speech自然告诉狗这次送往哪里并说明灯将显示的信号，不声称已设色或已送达。"
            "缺少可靠目的地或货物找到的可信依据就accepted=false，"
            "可将destination、color_name留空且color为null；不能提前猜站点。"
        )
    return common + (
        "现在由狗依据本单确切货物、灯已确认的配送区、最新真实工具回执选择下一步。"
        "找货的nearby是粗略位置判断，停稳回执不证明已经走到货物旁或已经贴上货物。"
        "按cargo_receipt中的实际移动距离和测量证据描述进展；没有移动证据不说已经走过去，"
        "没有接触或推送回执不说已经贴上或推过。"
        "可选observe重新观察、push按本次视觉距离推送、question向同伴询问、stop如实中止、finish结束。"
        "stage为waiting_destination时先报告发现或向灯提问，不能推送或宣称结束。"
        "只有destination_confirmed为true才可push；距离未明确要求时可交给推物工具估算。"
        "不因自己到站或移动过就推断货物送达。货物丢失、身份不明或路径受阻时先观察、询问或停止。"
        "question时question填写实际要问的问题；observe可用question说明需要核对的画面。"
        "具体站位和接近由推物工具完成，前置进展仍继续原目标。"
        "stage为directional_push_completed或delivered时执行器已核对本单完成判据，"
        "结束并只回复一次真实结果：前者仅为朝指定方向尝试推送并停稳，不能说货物已移动或送达；"
        "后者确认货物已在目的地附近；motion_performed=false时不得说本次执行了移动。"
    )


async def decide(role: str, phase: str, facts: dict, *, thread_id: str,
                 deadline: float, availability=None) -> dict:
    """Submit the stage contract to the lamp/Go2 brain, never a game model."""
    from .role_graph import decide as role_decide

    return await role_decide(role, phase, facts,
        schema=_schema(role, phase, facts.get("stage", "")),
        instructions=_instructions(phase), thread_id=thread_id,
        deadline=deadline, availability=availability)
