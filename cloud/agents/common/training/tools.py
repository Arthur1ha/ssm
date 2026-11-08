"""Training tools: preview through the authenticated API, never confirm."""
import asyncio
import base64
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, is_dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from cloud.agents.common.training.models import Definition

GUIDANCE = ('长期教导先用 list_training 读取已有教导和转换目录，只提出修改草案。'
            '定位唯一目标后少追问；多个目标须说明差别并澄清。慢一点、隔久一点等建议必须给出明确数值供用户确认。'
            '这次闪五次是临时命令；以后默认两次才是长期参数。改成半分钟应修改原教导，不创建竞争教导。'
            '没有能力时说明缺项，不能声称已经学会。草案须由用户确认，不能声称已保存或执行。')

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
    definition: Definition | None = None

SCHEMAS = {'list_training': ListTraining, 'propose_training': ProposeTraining}

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
    definition = args.get('definition')
    if selection and definition and selection['transition_id'] not in definition['transition_ids']:
        raise ValueError('selection_transition_mismatch')
    operation = args['operation']
    if selection and selection.get('teaching_id') and args.get('teaching_id') != selection['teaching_id']:
        raise ValueError('selection_teaching_mismatch')
    payload = {k: v for k, v in args.items() if k != 'base_revision'}
    payload['capability_hash'] = snapshot['catalog']['hash']
    if operation == 'create':
        payload['expected_revision'] = 0
        if not payload.get('name'):
            edges = {edge['id']: edge for edge in snapshot['catalog'].get('transitions', [])}
            ids = (definition or {}).get('transition_ids') or []
            if not ids or any(edge_id not in edges for edge_id in ids):
                raise ValueError('training_name_requires_known_transition')
            payload['name'] = '、'.join(f"{edges[edge_id]['src']} → {edges[edge_id]['dst']}" for edge_id in ids)[:120]

    else:
        record = next((r for r in snapshot['records'] if r['teaching_id'] == args.get('teaching_id')), None)
        if record is None or args.get('base_revision') != record['revision']:
            raise ValueError('teaching_revision_mismatch')
        if selection and selection['transition_id'] not in record['definition']['transition_ids']:
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

async def execute_training(name, arguments, context):
    try:
        value = await (list_training if name == 'list_training' else propose_training)(arguments, context, internal_api_call)
        return {'result': 'ok', **value}
    except Exception as exc:
        return {'result': 'rejected', 'error': str(exc)[:300]}
