"""A state-board request: confirm standing, then send one native action.

The LangGraph workflow uses the existing action boundary and effect ledger.
Request acceptance alone never authorizes the dependent action.
"""

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from typing import TypedDict

from fastapi import HTTPException
from langgraph.graph import END, START, StateGraph

from cloud.go2.actions import ACTIONS, SPORT_COMMANDS, STABLE_BODY_STATES
from cloud.go2.control import session_tasks
from cloud.go2.tasks import chat_tasks, command_identity
from cloud.go2.tools import executor


class _State(TypedDict, total=False):
    ready: bool
    result: dict
    preparation: dict
    motion_epoch: int


async def run_body_action(command, *, context, request_id, trace_id, deadline, epoch, motion_epoch):
    robot = executor.go2
    action = ACTIONS[command]

    def generation_matches(expected):
        # Disabling/unmounting the joystick can deliver a final zero after
        # StopMove. That idempotent release must not cancel the next posture.
        return robot._motion_epoch == expected or (
            robot._motion_epoch == robot._last_zero_velocity_epoch
            and not robot._velocity_command_active and not robot._move_control_active
            and not robot.pending_action)

    async def prepare(_state):
        snapshot = robot.state_snapshot()
        if snapshot['epoch'] != epoch or robot._motion_epoch != motion_epoch:
            return {'result': {'result': 'cancelled', 'error': '动作已被后续指令打断'}}
        reason = robot.runtime.active_reason() or robot.body_ready_reason()
        if reason or robot.pending_action:
            return {'result': {'result': 'blocked', 'error': reason or '上个动作的结果尚未确认'}}
        source = snapshot['body_state']
        if source == 'moving' and snapshot['control_mode'] == 'remote':
            # Leaving the movement node stops the wireless stick, then waits
            # for fresh stationary observations before the next body action.
            queue = robot.new_state_queue()
            try:
                stopped = await executor.execute_command_request(
                    'StopMove', {}, context=context, request_id=request_id + '_stop',
                    trace_id=trace_id, deadline=deadline)
                if stopped.get('result') not in {'ok', 'accepted'}:
                    return {'result': stopped}
                stop_epoch = robot._motion_epoch
                async with asyncio.timeout(min(5, max(0, deadline - time.time()))):
                    while True:
                        snapshot = robot.state_snapshot()
                        if snapshot['epoch'] != epoch or not generation_matches(stop_epoch):
                            return {'result': {'result':'cancelled','error':'停止移动已被后续指令打断'}}
                        if snapshot['body_state'] == 'standing' and not robot.body_ready_reason():
                            source = 'standing'
                            break
                        await queue.get()
            except TimeoutError:
                return {'result': {'result':'timeout','error':'尚未确认停稳，未执行后续动作'}}
            finally:
                robot.remove_state_queue(queue)
        if source in action.source_states:
            return {'ready': True, 'motion_epoch': robot._motion_epoch}
        if source not in STABLE_BODY_STATES or 'standing' not in action.source_states:
            return {'result': {'result': 'blocked', 'error': '当前姿态不能准备此动作'}}
        stand = next((spec for spec in ACTIONS.values()
                      if source in spec.source_states and spec.expected_state == 'standing'
                      and spec.execution_state is None and spec.completion == 'joint_posture'), None)
        if stand is None:
            return {'result': {'result': 'blocked', 'error': '当前姿态没有可用的起身动作'}}
        result = await executor.execute_command_request(
            stand.command, {}, context=context, request_id=request_id + '_stand',
            trace_id=trace_id, deadline=deadline)
        # A verified completion belongs to this physical request and connection.
        # The action boundary repeats ownership, freshness and posture checks.
        last = robot.runtime.snapshot().get('last_action') or {}
        if (result.get('result') != 'ok' or robot.runtime.data['epoch'] != epoch
                or last.get('request_id') != result.get('request_id')
                or last.get('result') != 'ok' or robot.body_ready_reason()
                or robot.body_state != 'standing'):
            return {'result': {'result': result['result'] if result['result'] not in {'ok', 'accepted', 'executing'} else 'blocked',
                               'error': '起身尚未确认，未执行后续动作', 'preparation': result}}
        return {'ready': True, 'preparation': result, 'motion_epoch': robot._motion_epoch}

    async def perform(state):
        if robot.runtime.data['epoch'] != epoch or not generation_matches(state['motion_epoch']):
            return {'result': {'result': 'cancelled', 'error': '连接或动作已变化，未执行后续动作'}}
        result = await executor.execute_command_request(
            command, {}, context=context, request_id=request_id + '_target',
            trace_id=trace_id, deadline=deadline,
            dispatch_only=command in SPORT_COMMANDS)
        return {'result': {**result, **({'preparation': state['preparation']} if state.get('preparation') else {})}}

    graph = StateGraph(_State)
    graph.add_node('prepare', prepare)
    graph.add_node('perform', perform)
    graph.add_edge(START, 'prepare')
    graph.add_conditional_edges('prepare', lambda state: 'perform' if state.get('ready') else END)
    graph.add_edge('perform', END)
    result = await graph.compile().ainvoke({})
    return result['result']


async def start_body_action(command, params, *, context, request_id=None, trace_id=None, deadline=None):
    executor.validate_execution_context(context)
    if (context.origin_kind != 'user_delegated' or params
            or command not in {*SPORT_COMMANDS, 'EnterMoveControl'} or command == 'StopMove'
            or ACTIONS[command].source_states != ('standing',)):
        raise HTTPException(status_code=400, detail='unsupported prepared body action')
    request_id = request_id or 'body_' + uuid.uuid4().hex
    try:
        identity = command_identity(context, request_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    task_id = 'body_' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
    trace_id = trace_id or 'tr_' + uuid.uuid4().hex
    if not re.fullmatch(r'tr_[A-Fa-f0-9]{32}', trace_id):
        raise HTTPException(status_code=400, detail='invalid trace_id')
    deadline = deadline if deadline is not None else time.time() + 45
    if not math.isfinite(deadline) or not time.time() < deadline <= time.time() + 300:
        raise HTTPException(status_code=400, detail='invalid deadline')
    if context.expires_at:
        deadline = min(deadline, context.expires_at)
    robot = executor.go2
    epoch, motion_epoch = robot.runtime.data['epoch'], robot._motion_epoch

    async def run():
        return await run_body_action(command, context=context, request_id=task_id,
                                     trace_id=trace_id, deadline=deadline, epoch=epoch,
                                     motion_epoch=motion_epoch)

    try:
        record = chat_tasks.start(task_id, context.session_id, 'body:' + command, deadline, run, trace_id=trace_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session_tasks.track(context.session_id, record.task)
    return await chat_tasks.wait(record, .2)
