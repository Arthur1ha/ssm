"""Strict serializable teaching definition and proposal contracts."""
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Condition(StrictModel):
    field: StrictStr
    op: Literal['eq', 'in']
    value: StrictBool | StrictStr | StrictInt | StrictFloat | list[StrictBool | StrictStr | StrictInt | StrictFloat]


class Trigger(StrictModel):
    event_id: StrictStr
    where: dict[str, Any] = Field(description='观察事件的条件，例如 recognized=true；只使用事件声明的字段')


class Step(StrictModel):
    transition_ids: list[StrictStr] = Field(min_length=1, description='本步骤各合法源状态的候选转换；普通步骤必须同动作，明确goal_state可包含到该目标的不同姿态动作；数组顺序不是执行顺序')
    conditions: list[Condition] = Field(default_factory=list, description='只约束本步骤所指转换的准入条件；主人身份必须来自可信请求者事实')
    defaults: dict[str, Any] = Field(default_factory=dict)
    goal_state: StrictStr | None = Field(default=None, description='仅明确姿态或生命周期目标使用；当前真实状态已满足时可跳过，不能用于打招呼等命令动作')


class Definition(StrictModel):
    steps: list[Step] = Field(min_length=1, max_length=12, description='有序的已有转换步骤；单动作也是仅含一步的教导')
    origins: list[Literal['user_delegated', 'agent_autonomous']] = Field(min_length=1)
    cooldown_s: StrictInt | StrictFloat | None = None
    trigger: Trigger | None = None
    # Interpreted by the role Graph, never used as a global device guard.
    situation: StrictStr | None = Field(default=None, min_length=1, max_length=2000,
        description="自然语言适用情境，由智能体语义理解；不要求传感器事件或固定口令")


class Draft(StrictModel):
    operation: Literal['create', 'update', 'remove']
    definition: Definition | None


class Confirmation(StrictModel):
    proposal_id: StrictStr
    expected_revision: StrictInt = Field(ge=0)


Identifier = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')]
CapabilityHash = Annotated[StrictStr, Field(pattern=r'^[a-f0-9]{64}$')]
Timestamp = Annotated[StrictInt | StrictFloat, Field(ge=0, allow_inf_nan=False)]


class ApplyTraining(StrictModel):
    """按上下文 training 或 list_training 返回的教导 ID 和当前版本应用保存的动作参数。

    用户表达当前情境时按语义选择适用教导；overrides 仅放本次明确修改的参数，
    不猜测或重填保存的整组配置。临时修改不改变原教导；长期修改用 propose_training。
    """
    unit_id: Identifier
    teaching_id: Identifier
    revision: StrictInt = Field(ge=1)
    overrides: dict[str, Any] = Field(default_factory=dict)


class Revision(StrictModel):
    schema_version: Literal[2]
    teaching_id: Identifier
    unit_id: Identifier
    space_id: Identifier
    name: StrictStr = Field(min_length=1, max_length=120)
    effect_text: StrictStr | None = Field(default=None, min_length=1, max_length=1200)
    source_text: StrictStr = Field(min_length=1, max_length=4000)
    revision: StrictInt = Field(ge=1)
    definition: Definition
    desired_status: Literal['enabled', 'disabled', 'removed']
    capability_hash: CapabilityHash
    loaded_revision: StrictInt | None = Field(ge=1)
    created_at: Timestamp
    updated_at: Timestamp

    @model_validator(mode='after')
    def valid_loaded_revision(self):
        if self.loaded_revision is not None and self.loaded_revision != self.revision:
            raise ValueError('loaded_revision must equal revision')
        if self.updated_at < self.created_at:
            raise ValueError('updated_at precedes created_at')
        return self


class Record(Revision):
    history: list[Revision] = Field(max_length=100)

    @model_validator(mode='after')
    def valid_history(self):
        if len(self.history) != self.revision - 1:
            raise ValueError('history must contain every previous revision')
        for index, old in enumerate(self.history, 1):
            if old.revision != index or any(getattr(old, key) != getattr(self, key)
                                           for key in ('teaching_id', 'unit_id', 'space_id', 'created_at')):
                raise ValueError('history identity or revision mismatch')
        return self


class ProposalRequest(StrictModel):
    unit_id: Identifier
    operation: Literal['create', 'update', 'remove']
    teaching_id: Identifier | None = None
    name: StrictStr | None = Field(default=None, min_length=1, max_length=120)
    effect_text: StrictStr | None = Field(default=None, min_length=1, max_length=1200)
    supersedes_proposal_id: Identifier | None = None
    definition: Definition | None = None
    expected_revision: StrictInt = Field(ge=0)
    capability_hash: CapabilityHash

    @model_validator(mode='after')
    def valid_operation(self):
        if self.operation == 'create':
            if self.teaching_id is not None or self.expected_revision != 0 or self.name is None or self.definition is None:
                raise ValueError('create requires name, definition, revision=0 and no teaching_id')
        elif self.teaching_id is None or self.expected_revision < 1:
            raise ValueError('update/remove requires teaching_id and positive expected_revision')
        if self.operation == 'update' and self.definition is None:
            raise ValueError('update requires definition')
        if self.operation == 'remove' and (self.definition is not None or self.name is not None or self.effect_text is not None):
            raise ValueError('remove cannot change name or definition')
        return self


class BrowserProposal(ProposalRequest):
    # Actual text of this authenticated browser request, never model provenance.
    user_text: StrictStr = Field(min_length=1, max_length=4000)


class Proposal(StrictModel):
    proposal_id: Identifier
    session_id: Identifier
    source_run_id: Identifier | None = None
    request: ProposalRequest
    record: Record
    created_at: Timestamp
    expires_at: Timestamp
    cancelled_at: Timestamp | None = None
    superseded_by: Identifier | None = None

    @model_validator(mode='after')
    def valid_binding(self):
        req, record = self.request, self.record
        if req.unit_id != record.unit_id or req.capability_hash != record.capability_hash:
            raise ValueError('proposal target/hash mismatch')
        if req.teaching_id is not None and req.teaching_id != record.teaching_id:
            raise ValueError('proposal teaching_id mismatch')
        if self.expires_at != self.created_at + 1800:
            raise ValueError('proposal must expire in 30 minutes')
        return self


class ConfirmRequest(StrictModel):
    expected_revision: StrictInt = Field(ge=0)
    capability_hash: CapabilityHash


class EnabledRequest(StrictModel):
    enabled: StrictBool
    expected_revision: StrictInt = Field(ge=1)


class LoadedVersion(StrictModel):
    revision: StrictInt = Field(ge=1)
    capability_hash: CapabilityHash


class LoadedRequest(StrictModel):
    executor_id: Literal['go2', 'lamp']
    revisions: dict[Identifier, LoadedVersion] = Field(max_length=500)


class TrustedSource(StrictModel):
    """Internal adapter metadata, never part of the model's arguments schema."""
    session_id: Identifier
    run_id: Identifier
    text: StrictStr = Field(min_length=1, max_length=4000)


class Partition(StrictModel):
    schema_version: Literal[2] = 2
    space_id: Identifier
    unit_id: Identifier
    records: dict[Identifier, Record] = Field(default_factory=dict, max_length=500)
    proposals: dict[Identifier, Proposal] = Field(default_factory=dict)
    confirmed_proposals: dict[Identifier, Record] = Field(default_factory=dict)
    loaded_epoch: dict[Identifier, Identifier] = Field(default_factory=dict)
    cursor: StrictInt = Field(default=0, ge=0)

    @model_validator(mode='after')
    def valid_partition(self):
        for key, record in self.records.items():
            if key != record.teaching_id:
                raise ValueError('record key mismatch')
        for key, proposal in self.proposals.items():
            if key != proposal.proposal_id:
                raise ValueError('proposal key mismatch')
        records = list(self.records.values()) + list(self.confirmed_proposals.values())
        records += [p.record for p in self.proposals.values()]
        if any(r.unit_id != self.unit_id or r.space_id != self.space_id for r in records):
            raise ValueError('partition identity mismatch')
        if set(self.loaded_epoch) - set(self.records):
            raise ValueError('unknown loaded teaching')
        return self
