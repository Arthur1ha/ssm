"""Training tools: preview through the authenticated API, never confirm."""
import asyncio
import base64
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, is_dataclass
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field
from cloud.agents.common.training.models import ApplyTraining, Definition
from cloud.agents.common.training.policy import application_parameters
from cloud.space.execution import ExecutionContext

GUIDANCE = ('通过现有工具理解任意自然语言行为规则；先用 list_training 读取已有规则、未完成草案和设备转换目录。'
            '由你结合用户原话、历史与事实理解情境和规则适用性，不依赖特定句式、关键词或预设场景清单。'
            'situation 是开放的自然语言适用情境，不是传感器事件；用户直接表达当前情境即可使用，不要求设备自动识别。'
            '依据用户意图选择真实支持的动作转换与参数；conditions 只放适用后必须遵守的明确限制。'
            '用户说以后某场景用现在这档颜色时，把真实当前 RGB 和亮度保存到 defaults，情境保存到 situation；'
            '不要改成所有开灯或调色都使用这档，也不新增硬件场景状态。'
            '用户只指定情境与动作而未限制起始状态时，绑定该动作在各合法起始状态的等价转换；不要仅因当前状态固化适用起点。'
            '用户表达进入某情境时，语义选择对应已保存规则并用 apply_training 按 ID 和当前版本调用；'
            'overrides 只放用户本次明确修改的参数，不重填或猜测保存的整组颜色。'
            '用户说以后都这样时修订原规则；只说这次时使用本次 overrides，不改变保存配置。'
            '每次 create/update 都必须重新给出 name：一句简短、完整、用户可读的规则说明，通常20个汉字以内，适合放在状态转换卡片；'
            '不重复卡片已有的状态名称，不列场景清单，不用技术字段、枚举值或泛泛的标题，也不能截断关键限制来凑字数。'
            'effect_text 用简短自然语言说明什么情况下会做什么、限制了什么，完整对应结构化定义，不输出参数或 JSON。'
            '同一要求的补充必须修改原规则；已有草案时使用 supersedes_proposal_id 修订该草案，未确认的新增草案仍为 create。'
            '唯一目标少追问，多个目标才澄清；区分临时动作与长期约定，模糊数值给出具体建议供确认。'
            '只约束用户指明的转换和方向；转换自身的起点已确定时，不把它误加为其它转换的全局允许条件。'
            '用户说仅在某种情境下限制行为，要区别规则何时适用和动作何时允许，不得把其它情境的动作一起禁止。'
            '制定规则不要求当前已经满足条件；条件未知时不能编造事实，也不能把当前未触发误说成无法制定规则。'
            '不要在回复中重复解释卡片已经展示的技术过程。草案由用户在对话卡片确认，不能提前声称规则已保存或行为已改变。'
            '超出设备当前结构化能力时简短说明具体缺项，不编造转换或识别结果。')

class ListTraining(BaseModel):
    """读取设备可训练转换和已有教导。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    unit_id: str

class ProposeTraining(BaseModel):
    """提出长期教导修改草案；不保存、不确认、不执行。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    operation: Literal['create', 'update', 'remove']
    unit_id: str
    teaching_id: str | None = None
    base_revision: int | None = Field(default=None, ge=1)
    name: str | None = None
    effect_text: str | None = Field(default=None, min_length=1, max_length=1200)
    supersedes_proposal_id: str | None = None
    definition: Definition | None = None

SCHEMAS = {'list_training': ListTraining, 'propose_training': ProposeTraining,
           'apply_training': ApplyTraining}

def _selection(context, unit_id):
    selection = context.get('selection')
    if selection and selection.get('unit_id') != unit_id:
        raise ValueError('selection_unit_mismatch')
    return selection

async def list_training(arguments, context, api_call):
    args = ListTraining.model_validate(arguments).model_dump()
    _selection(context, args['unit_id'])
    path = '/api/training?' + urllib.parse.urlencode({'unit_id': args['unit_id']})
    return await api_call(path, None, context, method='GET')

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
    selected_proposal = (selection or {}).get('proposal_id')
    supersedes = args.get('supersedes_proposal_id') or selected_proposal
    if selected_proposal and supersedes != selected_proposal:
        raise ValueError('selection_proposal_mismatch')
    if supersedes:
        previous = next((p for p in snapshot.get('proposals', []) if p['proposal_id'] == supersedes), None)
        if previous is None or previous.get('confirmed') or previous.get('cancelled') or previous.get('superseded') or previous.get('expired'):
            raise ValueError('proposal_not_editable')
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
    if bare_transition and definition and selection['transition_id'] not in definition['transition_ids']:
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
        if selection and not selection.get('proposal_id') and selection['transition_id'] not in record['definition']['transition_ids']:
            raise ValueError('selection_transition_mismatch')
        payload['expected_revision'] = args['base_revision']
    return await api_call('/api/training/proposals', payload, context, method='POST')

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
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as response:
            return json.load(response)
    return await asyncio.to_thread(request)

async def prepare_training_application(
        arguments: dict, context: ExecutionContext | dict,
        api_call: Callable | None = None) -> dict:
    """Read saved parameters; the caller still owns actual device execution.

    Bind the returned reference and overrides to ExecutionContext before calling
    the original executor, which rechecks the record under its dispatch lock.
    """
    args = ApplyTraining.model_validate(arguments).model_dump()
    context = asdict(context) if is_dataclass(context) else context
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
    action, params = application_parameters(record, snapshot['catalog'], args['overrides'])
    return {'unit_id': args['unit_id'], 'action': action, 'params': params,
            'training_ref': record['teaching_id'], 'training_revision': record['revision'],
            'training_overrides': args['overrides']}


async def execute_training(name, arguments, context):
    try:
        adapters = {'list_training': list_training, 'propose_training': propose_training}
        if name not in adapters:
            raise ValueError('unknown_training_tool')
        value = await adapters[name](arguments, context, internal_api_call)
        return {'result': 'ok', 'result_kind': ('training_query' if name == 'list_training'
                                               else 'training_proposal'), **value}
    except Exception as exc:
        return {'result': 'rejected', 'error': str(exc)[:300]}
