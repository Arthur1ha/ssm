"""Fresh camera evidence waits stay inside the original teaching execution."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import time

import pytest

from cloud.agents.common.training import store
from cloud.agents.common.training.catalog import catalog_for
from cloud.agents.common.training.workflow import run_training_workflow
from cloud.go2 import training_events as events
from cloud.go2.card import _build_go2_card
from cloud.go2.command_request_checks import device
from cloud.go2.observation_scheduler_checks import scheduled
from cloud.go2.training_event_checks import listener, bound_listener
from cloud.space.execution import ExecutionContext


def stale_context(f):
    worker = f.adapter._target_listeners['书本']
    worker._presence = worker._visible_latched = True
    worker._frame_ts = time.time() - 6
    return worker, replace(worker._context, trigger_ref='书本', trigger_revision=1,
        trigger_mode_epoch=worker._generation, training_run_id='original-run',
        goal_revision=events.get_goal_registry().current()['revision'],
        expires_at=int(time.time() + 60))


@pytest.mark.asyncio
@pytest.mark.parametrize('visible', [True, False, None])
async def test_stale_evidence_requests_a_fresh_frame_and_never_invents_presence(scheduled, monkeypatch, visible):
    f = scheduled
    f.add('书本')
    await f.sample(100)
    worker, context = stale_context(f)
    original_ts = worker._frame_ts
    from cloud.go2.perception import teaching_targets

    async def observe(_jpeg, _frame, targets):
        return {target: visible for target in targets}

    monkeypatch.setattr(teaching_targets, 'observe_targets', observe)
    waiting = asyncio.create_task(worker.refresh_observation(context))
    await asyncio.sleep(0)
    assert f.adapter._target_refresh == {'书本'}
    # An unrelated update cannot consume the pending evidence wait.
    f.adapter._notify_target_update()
    await asyncio.sleep(.01)
    assert not waiting.done()
    await f.sample(103)
    result = await asyncio.wait_for(waiting, 1)
    if visible is True:
        assert result['values'] == {'target': '书本', 'detected': True}
        assert result['ts'] > original_ts and result['frame_id'] == 2
    else:
        assert result is None
    assert not f.adapter._target_refresh


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke', ['goal', 'takeover', 'timeout'])
async def test_evidence_wait_exits_on_revocation_without_waiting_for_fallback(scheduled, revoke):
    f = scheduled
    f.add('书本')
    await f.sample(100)
    worker, context = stale_context(f)
    waiting = asyncio.create_task(worker.refresh_observation(context))
    await asyncio.sleep(0)
    if revoke == 'goal':
        events.get_goal_registry().advance('user', 'new-goal')
    elif revoke == 'takeover':
        f.adapter.interrupt()
    else:
        f.clock.now = 111
        f.adapter._notify_target_update()
    assert await asyncio.wait_for(waiting, 1) is None
    assert not f.adapter._target_refresh
    assert len(f.requests) == 1 and not f.calls


def saved_behavior(f, actions):
    catalog = catalog_for(_build_go2_card())
    record = deepcopy(f['records'][0])
    record['definition']['cooldown_s'] = None
    record['definition']['steps'] = []
    for action in actions:
        edge = next(item for item in catalog['transitions']
                    if item['action'] == action and item['src'] == 'standing')
        step = {'transition_ids': [edge['id']], 'conditions': [], 'defaults': {}}
        if action == 'Sit':
            step['goal_state'] = 'sitting'
        record['definition']['steps'].append(step)
    with store.partition_lock('go2', space_id='main', write=True) as data:
        data['records'][record['teaching_id']] = record
    return {'unit_id': 'go2', 'record': record, 'steps': record['definition']['steps'],
            'training_ref': record['teaching_id'], 'training_revision': 1,
            'training_overrides': {}}


@pytest.mark.asyncio
async def test_workflow_reads_post_refresh_posture_before_deciding_goal_is_already_met(bound_listener):
    _adapter, robot, _lease, f = bound_listener
    prepared = saved_behavior(f, ['Sit'])
    robot.runtime.body('sitting', confirmation='observed')
    calls = []

    async def refresh(_context):
        robot.runtime.body('standing', confirmation='observed')

    async def execute(action, _params, _context, request_id):
        calls.append((action, request_id))
        return {'result': 'ok'}

    result = await run_training_workflow(prepared, ExecutionContext('refresh', 'visitor', False),
        run_id='post-refresh-pose', snapshot=robot.state_snapshot, execute=execute,
        refresh_observation=refresh)
    assert result['result'] == 'ok'
    assert [action for action, _request in calls] == ['Sit']


@pytest.mark.asyncio
async def test_failed_second_step_refresh_preserves_first_receipt_and_cannot_restart_on_new_evidence(bound_listener):
    _adapter, robot, _lease, f = bound_listener
    prepared = saved_behavior(f, ['Hello', 'Sit'])
    calls = []
    checks = []

    async def refresh(context):
        checks.append(context.training_step_index)
        if context.training_step_index == 1:
            return {'result': 'blocked', 'error': 'training_observation_unavailable'}

    async def execute(action, _params, _context, request_id):
        calls.append((action, request_id))
        return {'result': 'ok'}

    fields = dict(run_id='original-two-step', snapshot=robot.state_snapshot,
                  execute=execute, refresh_observation=refresh)
    context = ExecutionContext('refresh', 'visitor', False)
    first = await run_training_workflow(prepared, context, **fields)
    second = await run_training_workflow(prepared, context, **fields)
    assert first['result'] == second['result'] == 'blocked'
    assert [action for action, _request in calls] == ['Hello']
    assert checks == [0, 1]
    assert second['replayed'] is True
