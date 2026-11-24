"""Training tools: preview through the authenticated API, never confirm."""
import asyncio
import base64
import json
import os
import re
import time
import urllib.parse
import urllib.error
import urllib.request
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field
from cloud.agents.common.training.models import ApplyTraining, Definition
from cloud.agents.common.training.policy import definition_transition_ids, validate_definition, step_parameters
from cloud.space.execution import ExecutionContext

class ListTraining(BaseModel):
    """只读查询设备状态转移目录、当前未删除教导和未确认草案。

    制定或修订长期约定前先查询；用户表达当前情境时按语义选择已有教导，
    用 apply_training 应用。不要求固定句式或设备自动识别情境。
    records 不含已删除教导和历史版本；disabled 表示未启用，不能当作当前生效教导。
    proposals 的 status 和 confirmation_allowed 表示草案是否仍可确认。
    查询不保存、不执行；空 records/proposals 不代表有待清理旧教导，出错时不猜测。
    """
    model_config = ConfigDict(extra='forbid', strict=True)
    unit_id: str

class ProposeTraining(BaseModel):
    """提出长期教导草案，待用户在卡片确认；不保存或执行动作。

    先用 list_training 查询目录和已有记录，只引用真实支持的转换。
    definition.steps 是唯一有序执行定义，单动作也只有一步。每步 transition_ids 是各合法
    源状态的候选，conditions 约束该步骤，defaults 是该步骤的真实动作参数。
    普通步骤仅同动作；明确姿态goal可引用到同一目标的不同合法姿态动作，例如
    站立goal同时绑定躺姿起身与坐姿起身，按实际姿态选择；候选数组顺序不是执行顺序。
    只有明确姿态或生命周期目标才设置 goal_state，且必须符合目录 goal_eligible；
    不可用 standing 等终态跳过打招呼、调色、有限闪烁等命令。
    situation 仅描述需要模型选择的开放情境；全入口硬约束必须 situation=null，
    将“只有主人能唤醒”放 wake 步骤的 requester_is_owner 条件，不能塞进 situation。
    未限定请求来源的全入口硬约束，origins 同时包含 user_delegated 和 agent_autonomous。
    参数情境未选中不影响其他请求；条件只限制其绑定转换，不扩散到其他步骤。
    要求观察后主动执行必须绑定真实 trigger 且 origins 含 agent_autonomous；
    “你开心时做动作”等自身情绪表达设置 definition.emotion，复用回复的情绪枚举，
    不设置观察 trigger；无额外情境时 situation=null，按用户要求保存完整动作参数。
    情绪表达未限定来源时 origins 包含 user_delegated 和 agent_autonomous，覆盖对话与活动；
    用户明确只在聊天或只在自主活动中表达时尊重其范围，仍由用户确认草案后生效。
    情绪映射与观察触发不可混用；用户开心等他人情绪不得当作自身 emotion。
    观察条件只放 trigger.where，动作参数一律放 steps 中的 defaults。
    看到书本、食物等物体时，用目录中的 vision.target_presence，where={target:具体物体描述,detected:true}；
    目标作为教导参数动态增加，不自造 vision.book 等事件。此观察仅判断画面中可见物体，不能识别人物身份或不可见意图。
    看到陌生人用 vision.stranger_recognized，where={recognized:true}，必须来自本次有效身份识别未匹配登记人员。
    主人和陌生人身份不使用通用目标观察替代；没有相应观察或动作能力时说明缺项，不静默缩减用户目标。
    同一要求更新原教导；补充草案用 supersedes_proposal_id，保留原 operation。
    create/update 给出完整 name 和 effect_text，文字完整对应条件、顺序及参数。
    失败重试必须保留触发器、身份限制和动作顺序，修正参数或如实说明能力缺项。
    制定规则无需当前已经满足条件；不得编造请求者身份或额外插入未授权动作。
    忠实保存用户要求，不自行添加执行间隔、冷却时间、持续在场条件、重复次数或准备动作。
    观察 trigger 仅启动整条 steps 链，不要求原观察在后续步骤始终成立。
    用户未明确要求执行间隔时，新规则 cooldown_s=null；修改其他内容时保留原间隔。
    同一观察事件去重和执行中避免并发由运行层处理，不得为“避免重复”自行设置60秒等间隔。
    """
    model_config = ConfigDict(extra='forbid', strict=True)
    operation: Literal['create', 'update', 'remove']
    unit_id: str
    teaching_id: str | None = None
    base_revision: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, description="简短完整的规则说明，通常20个汉字以内，不截断关键限制")
    effect_text: str | None = Field(default=None, min_length=1, max_length=1200)
    supersedes_proposal_id: str | None = None
    definition: Definition | None = None

SCHEMAS = {'list_training': ListTraining, 'propose_training': ProposeTraining,
           'apply_training': ApplyTraining}


class TrainingAPIError(ValueError):
    def __init__(self, code, *, status_code, details=None):
        super().__init__(code)
        self.status_code, self.details = status_code, details


_DURATION_NUMBER = r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+|半)'
_DURATION_UNIT = r'(?:毫秒|秒钟?|分钟?|小时)'
_INTERVAL_DURATION = re.compile(
    rf'(?:冷却(?:时间)?|(?:执行|触发|重复)?间隔(?:时间)?|隔)\s*'
    rf'(?:(?:改|设|设置|调整)(?:为|成|到)|为|是|用|至少|最少|要|需要)?\s*'
    rf'(?P<number>{_DURATION_NUMBER})\s*(?P<unit>{_DURATION_UNIT})')
_REPEAT_DURATION = re.compile(
    rf'每\s*(?P<number>{_DURATION_NUMBER})\s*(?P<unit>{_DURATION_UNIT})\s*'
    r'(?:最多|只|才)?(?:执行|触发|做|运行)?(?:一|1)次')
_REMOVE_INTERVAL = re.compile(
    r'(?:取消|去掉|移除|删除|不要|不用|无需|不设置|不设|不加)\s*(?:这个|那个|这条规则的|执行|触发|任何|所有|的)*\s*'
    r'(?:冷却(?:时间)?|间隔(?:时间)?)'
    r'|(?:冷却(?:时间)?|(?:执行|触发)?间隔(?:时间)?)\s*(?:取消|去掉|移除|删除|不要|不用|无需|不设)')


def _duration_seconds(number: str, unit: str) -> float:
    """Read only the duration bound to an explicit repeat-interval request."""
    if number == '半':
        value = .5
    elif number[0].isdigit():
        value = float(number)
    else:
        digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
        section = value = digit = 0
        for char in number:
            if char in digits:
                digit = digits[char]
            elif char == '万':
                value += (section + digit) * 10000
                section = digit = 0
            else:
                section += (digit or 1) * {'十': 10, '百': 100, '千': 1000}[char]
                digit = 0
        value += section + digit
    scale = .001 if unit == '毫秒' else 3600 if unit == '小时' else 60 if unit.startswith('分') else 1
    return value * scale


def _check_requested_cooldown(definition, previous, source_text):
    """Model proposals cannot invent or change a saved repeat interval.

    The source comes from the authenticated role turn, never model arguments.
    Action durations and unrelated numbers do not authorize a repeat interval.
    Unrecognized wording fails with corrective feedback rather than guessing.
    """
    proposed = definition.get('cooldown_s')
    baseline = (previous or {}).get('cooldown_s')
    source = source_text if isinstance(source_text, str) else ''
    requested = [_duration_seconds(match['number'], match['unit'])
                 for pattern in (_INTERVAL_DURATION, _REPEAT_DURATION)
                 for match in pattern.finditer(source)]
    expected = baseline
    if _REMOVE_INTERVAL.search(source):
        expected = None
        if proposed is None:
            return
    elif len(set(requested)) == 1:
        expected = requested[0]
        if proposed == requested[0]:
            return
    elif not requested and proposed == baseline:
        return
    raise ValueError('cooldown_not_requested: do not add or change an execution interval without '
                     f'an explicit user interval request; use cooldown_s={json.dumps(expected)} '
                     'to match this request and the prior definition; a new rule defaults to null, '
                     'and action duration is not a repeat interval')


def _preserve_event_request(arguments, attempts, catalog):
    """Keep the latest utterance's already-established behavior on tool retries."""
    definition = arguments.get('definition') or {}
    edges = {edge['id']: edge for edge in catalog.get('transitions', [])}
    def actions(step):
        return {(edges[key]['action'], json.dumps(edges[key].get('selector_params', {}), sort_keys=True))
                for key in step.get('transition_ids', []) if key in edges and edges[key].get('action')}
    new_steps = definition.get('steps', [])
    for attempt in attempts:
        old = attempt.get('params') or {}
        if (attempt.get('tool') != 'propose_training' or old.get('unit_id') != arguments.get('unit_id')
                or old.get('operation') != arguments.get('operation')
                or old.get('teaching_id') != arguments.get('teaching_id')):
            continue
        before = old.get('definition') or {}
        old_steps = before.get('steps', [])
        if not old_steps:
            continue
        old_actions = set().union(*(actions(step) for step in old_steps))
        new_actions = set().union(*(actions(step) for step in new_steps)) if new_steps else set()
        related = (bool(old.get('teaching_id')) or old.get('name') == arguments.get('name')
                   or bool(old_actions & new_actions))
        if not related:
            continue
        if before.get('emotion') is not None and before['emotion'] != definition.get('emotion'):
            raise ValueError('training_emotion_changed_on_retry: preserve the requested emotional expression')
        trigger, updated = before.get('trigger'), definition.get('trigger')
        if trigger and (not updated or updated.get('event_id') != trigger.get('event_id')
                or any((updated.get('where') or {}).get(key) != value
                       for key, value in (trigger.get('where') or {}).items())):
            raise ValueError('event_trigger_changed_on_retry: preserve the requested event and observation conditions')
        if not set(before.get('origins', [])) <= set(definition.get('origins', [])):
            raise ValueError('training_scope_weakened_on_retry')
        if (before.get('situation') is None and before.get('emotion') is None
                and definition.get('situation') is not None):
            raise ValueError('global_guard_became_optional_situation_on_retry')
        if len(old_steps) != len(new_steps):
            raise ValueError('training_sequence_changed_on_retry: preserve all requested steps and their order')
        for previous, current in zip(old_steps, new_steps):
            previous_actions, current_actions = actions(previous), actions(current)
            if previous_actions and current_actions and not previous_actions & current_actions:
                raise ValueError('training_sequence_changed_on_retry')
            for condition in previous.get('conditions', []):
                if condition not in current.get('conditions', []):
                    raise ValueError('training_guard_removed_on_retry: preserve identity and transition conditions')
            if previous.get('goal_state') != current.get('goal_state'):
                raise ValueError('training_goal_changed_on_retry')


def _selection(context, unit_id):
    selection = context.get('selection')
    if selection and selection.get('unit_id') != unit_id:
        raise ValueError('selection_unit_mismatch')
    return selection

_MODEL_RECORD_FIELDS = (
    'schema_version', 'unit_id', 'space_id', 'teaching_id', 'revision',
    'capability_hash', 'name', 'effect_text', 'definition', 'desired_status',
    'loaded_revision', 'saved', 'load_status', 'executable', 'execution_reasons',
    'transition_ids', 'transitionLabel', 'summaryText', 'changeText', 'scopeText',
    'stepSummaries',
)
_MODEL_PROPOSAL_FIELDS = (
    'proposal_id', 'operation', 'unit_id', 'teaching_id', 'expected_revision',
    'capability_hash', 'expires_at', 'confirmation_allowed', 'reasons',
    'confirmed', 'cancelled', 'superseded', 'superseded_by', 'expired',
    'beforeSummaryText', 'afterSummaryText', 'transition_ids', 'transitionLabel',
    'summaryText', 'changeText', 'scopeText', 'stepSummaries',
)


def _model_record(record):
    return {key: deepcopy(record[key]) for key in _MODEL_RECORD_FIELDS if key in record}


def _model_proposal(proposal):
    result = {key: deepcopy(proposal[key]) for key in _MODEL_PROPOSAL_FIELDS if key in proposal}
    for key in ('before', 'after'):
        if key in proposal:
            result[key] = _model_record(proposal[key]) if proposal[key] is not None else None
    result['status'] = (
        'confirmed' if proposal.get('confirmed') else
        'cancelled' if proposal.get('cancelled') else
        'superseded' if proposal.get('superseded') or proposal.get('superseded_by') else
        'expired' if proposal.get('expired') else
        'blocked' if proposal.get('confirmation_allowed') is False or proposal.get('reasons') else 'pending'
    )
    return result


def _model_snapshot(snapshot):
    """Project current configuration; management history stays in its API/store."""
    return {
        **{key: deepcopy(snapshot[key]) for key in ('unit_id', 'space_id', 'catalog', 'cursor') if key in snapshot},
        'records': [_model_record(record) for record in snapshot.get('records', [])
                    if record.get('desired_status') != 'removed'],
        'proposals': [_model_proposal(proposal) for proposal in snapshot.get('proposals', [])],
    }


async def list_training(arguments, context, api_call):
    args = ListTraining.model_validate(arguments).model_dump()
    _selection(context, args['unit_id'])
    path = '/api/training?' + urllib.parse.urlencode({'unit_id': args['unit_id']})
    snapshot = await api_call(path, None, context, method='GET')
    return _model_snapshot(snapshot)

async def propose_training(arguments, context, api_call):
    # The shared Graph runner omits nulls after schema validation. Restore
    # required nullable Definition fields before the adapter revalidates.
    arguments = dict(arguments)
    if isinstance(arguments.get('definition'), dict):
        definition = dict(arguments['definition'])
        definition.setdefault('cooldown_s', None)
        definition.setdefault('trigger', None)
        arguments['definition'] = definition
    validated = ProposeTraining.model_validate(arguments)
    args = validated.model_dump(exclude_none=True)
    if validated.definition is not None:
        args['definition'] = validated.definition.model_dump()
    selection = _selection(context, args['unit_id'])
    snapshot = await list_training({'unit_id': args['unit_id']}, context, api_call)
    _preserve_event_request(args, context.get('proposal_attempts', []), snapshot['catalog'])
    selected_proposal = (selection or {}).get('proposal_id')
    supersedes = args.get('supersedes_proposal_id') or selected_proposal
    previous_definition = None
    if selected_proposal and supersedes != selected_proposal:
        raise ValueError('selection_proposal_mismatch')
    if supersedes:
        previous = next((p for p in snapshot.get('proposals', []) if p['proposal_id'] == supersedes), None)
        if previous is None or previous.get('confirmed') or previous.get('cancelled') or previous.get('superseded') or previous.get('expired'):
            raise ValueError('proposal_not_editable')
        previous_definition = (previous.get('after') or {}).get('definition')
        args['supersedes_proposal_id'] = supersedes
        args['operation'] = previous['operation']
        if previous['operation'] != 'create':
            args['teaching_id'] = previous['teaching_id']
            args['base_revision'] = previous['expected_revision']
        else:
            args.pop('teaching_id', None)
            args.pop('base_revision', None)
    definition = args.get('definition')
    bare_transition = selection and not selection.get('teaching_id') and not selection.get('proposal_id')
    if bare_transition and definition and selection['transition_id'] not in definition_transition_ids(definition):
        raise ValueError('selection_transition_mismatch')
    operation = args['operation']
    if operation in ('create', 'update'):
        if not args.get('name') or not args['name'].strip():
            raise ValueError('rule_summary_required: provide a concise name describing the complete updated rule')
        # Never carry an old explanation over a changed definition. Both texts
        # are generated in this role Graph turn; an omitted detail uses its new
        # concise rule sentence rather than an unrelated historical description.
        args.setdefault('effect_text', args['name'])
    if selection and selection.get('teaching_id') and args.get('teaching_id') != selection['teaching_id']:
        raise ValueError('selection_teaching_mismatch')
    payload = {k: v for k, v in args.items() if k != 'base_revision'}
    payload['capability_hash'] = snapshot['catalog']['hash']
    if operation == 'create':
        payload['expected_revision'] = 0
    else:
        record = next((r for r in snapshot['records'] if r['teaching_id'] == args.get('teaching_id')), None)
        if record is None or args.get('base_revision') != record['revision']:
            raise ValueError('teaching_revision_mismatch')
        if selection and not selection.get('proposal_id') and selection['transition_id'] not in definition_transition_ids(record['definition']):
            raise ValueError('selection_transition_mismatch')
        payload['expected_revision'] = args['base_revision']
        if previous_definition is None:
            previous_definition = record['definition']
    if definition is not None:
        _check_requested_cooldown(definition, previous_definition, context.get('source_text'))
    proposal = await api_call('/api/training/proposals', payload, context, method='POST')
    return _model_proposal(proposal)

async def internal_api_call(path, payload, context, *, method='POST'):
    """The caller owns context; model arguments cannot supply source or authority."""
    context = asdict(context) if is_dataclass(context) else context
    if (context.get('actor') not in {'admin', 'visitor'} or
        context.get('origin_kind', 'user_delegated') != 'user_delegated' or
        not context.get('expires_at') or context['expires_at'] <= time.time()):
        raise ValueError('authenticated_user_context_required')
    if method != 'GET' and (context['actor'] != 'admin' or not context.get('persist')):
        raise ValueError('administrator_user_context_required')
    headers = {'Content-Type': 'application/json', 'X-SSM-Service-Token': os.environ.get('SSM_INTERNAL_SERVICE_TOKEN', ''),
               'X-SSM-Session-Id': context['session_id'], 'X-SSM-Actor': context['actor'],
               'X-SSM-Persist': 'true' if context.get('persist') else 'false', 'X-SSM-Space-Id': context.get('space_id', 'main'),
               'X-SSM-Origin-Kind': 'user_delegated', 'X-SSM-Expires-At': str(int(context['expires_at']))}
    if method == 'POST':
        source = {'session_id': context['session_id'], 'run_id': context['run_id'], 'text': context['source_text']}
        headers['X-SSM-Training-Source'] = base64.b64encode(json.dumps(source, ensure_ascii=False).encode()).decode()
    def request():
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        base = os.getenv('SSM_INTERNAL_API_BASE_URL', 'http://127.0.0.1:8082').rstrip('/')
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read(65536)).get('detail', {})
            except (ValueError, AttributeError):
                detail = {}
            code = detail.get('code') if isinstance(detail, dict) else None
            raise TrainingAPIError(code or 'training_api_rejected', status_code=exc.code,
                                   details=detail) from exc
    return await asyncio.to_thread(request)

async def prepare_training_application(
        arguments: dict, context: ExecutionContext | dict,
        api_call: Callable | None = None) -> dict:
    """Read saved parameters; the caller still owns actual device execution.

    Bind the returned reference and overrides to ExecutionContext before calling
    the original executor, which rechecks the record under its dispatch lock.
    """
    args = ApplyTraining.model_validate(arguments).model_dump()
    trusted = context
    context = asdict(context) if is_dataclass(context) else context
    if context.get('origin_kind') == 'agent_autonomous':
        if (not isinstance(trusted, ExecutionContext) or trusted.actor not in {'agent', 'visitor'}
                or trusted.persist is not False or trusted.space_id != 'main'
                or not trusted.session_id or (trusted.expires_at and trusted.expires_at <= time.time())):
            raise ValueError('trusted_autonomous_context_required')
        from cloud.agents.common.training import service, store
        from cloud.agents.common.training.catalog import catalog_for
        from cloud.agents.common.runner import invoke_blocking

        def read_current():
            card = service._target(args['unit_id'], trusted.space_id)
            return {'catalog': catalog_for(card),
                    'records': store.list_records(args['unit_id'], space_id=trusted.space_id)}

        snapshot = await invoke_blocking(read_current)
    else:
        snapshot = await list_training({'unit_id': args['unit_id']}, context,
                                       api_call or internal_api_call)
    record = next((row for row in snapshot['records']
                   if row['teaching_id'] == args['teaching_id']), None)
    if record is None or record.get('desired_status') != 'enabled':
        raise ValueError('training_not_enabled')
    if record['revision'] != args['revision']:
        raise ValueError('training_revision_changed')
    if record.get('unit_id') != args['unit_id']:
        raise ValueError('training_target_mismatch')
    if record.get('capability_hash') != snapshot['catalog']['hash']:
        raise ValueError('capability_changed')
    if context.get('origin_kind', 'user_delegated') not in record['definition']['origins']:
        raise ValueError('training_origin_mismatch')
    catalog = snapshot['catalog']
    definition = validate_definition(record['definition'], catalog)
    if definition['trigger']:
        raise ValueError('event_training_requires_event')
    if len(definition['steps']) > 1 and args['overrides']:
        raise ValueError('ambiguous_overrides: multi-step parameters must be revised in a complete teaching draft')
    for index in range(len(definition['steps'])):
        step_parameters(record, catalog, index, args['overrides'])
    return {'unit_id': args['unit_id'], 'record': record, 'catalog': catalog,
            'steps': definition['steps'],
            'training_ref': record['teaching_id'], 'training_revision': record['revision'],
            'training_overrides': args['overrides']}


async def execute_training(name, arguments, context):
    try:
        adapters = {'list_training': list_training, 'propose_training': propose_training}
        if name not in adapters:
            raise ValueError('unknown_training_tool')
        value = await adapters[name](arguments, context, internal_api_call)
        return {'result': 'ok', 'result_kind': ('query' if name == 'list_training'
                                               else 'training_proposal'), **value}
    except TrainingAPIError as exc:
        return {'result': 'rejected', 'error': str(exc),
                'status_code': exc.status_code, 'details': exc.details}
    except Exception as exc:
        return {'result': 'rejected', 'error': str(exc)[:300]}
