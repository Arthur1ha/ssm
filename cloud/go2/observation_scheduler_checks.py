"""Teaching-camera scheduling against simulated fresh frames and rule storage."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import math
import time
from types import SimpleNamespace

import pytest

from cloud.go2 import training_events as events
from cloud.go2.perception.observation_schedule import TargetObservationSchedule
from cloud.go2.training_event_checks import listener


@pytest.fixture
def scheduled(listener, monkeypatch, tmp_path):
    for key in ('SSM_USER_GOALS_DIR', 'SSM_TRAINING_DIR', 'SSM_EFFECT_LEDGER_DIR',
                'SSM_CHECKPOINT_DIR', 'SSM_RUN_INDEX_DIR'):
        monkeypatch.setenv(key, str(tmp_path / key))
    adapter, robot, snapshot, _drive, calls = listener
    snapshot['activity_state'] = 'active'
    records = events.list_records('go2')
    template = deepcopy(records[0])
    records.clear()
    clock = SimpleNamespace(now=100.0)
    # Replace this module's clock reference, never asyncio's monotonic clock.
    monkeypatch.setattr(events, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock.now))

    class Gate:
        def __init__(self):
            self.baseline = None

        def observe(self, jpeg, *, frame_id):
            return SimpleNamespace(changed=self.baseline != jpeg, token=jpeg)

        def mark_analyzed(self, token):
            self.baseline = token
            return True

    monkeypatch.setattr(events, 'SceneChangeGate', Gate)
    adapter._target_scene = Gate()
    adapter._target_schedule = TargetObservationSchedule()
    from cloud.go2.perception import teaching_targets, vision
    image = SimpleNamespace(content=b'unchanged scene')
    monkeypatch.setattr(vision.vision_loop, 'get_frame', lambda _frame_id: image.content)
    requests = []
    response = SimpleNamespace(error=None, entered=None, finish=None, detected=False)

    async def observe(_jpeg, frame, targets):
        requests.append((clock.now, frame['frame_id'], tuple(targets)))
        if response.entered is not None:
            response.entered.set()
            await response.finish.wait()
        if response.error is not None:
            raise response.error
        return {target: response.detected for target in targets}

    monkeypatch.setattr(teaching_targets, 'observe_targets', observe)

    def add(target):
        record = deepcopy(template)
        record.update(teaching_id=target, name='看到' + target + '就挥手')
        record['definition']['trigger'] = {
            'event_id': 'vision.target_presence', 'where': {'target': target, 'detected': True}}
        records.append(record)
        return record

    counter = 0

    async def sample(now, *, wait=True):
        nonlocal counter
        clock.now = now
        counter += 1
        frame = {'frame_id': counter, 'source_frame_id': counter,
                 'camera_session_id': 'scheduled-camera', 'ts': time.time() - .01,
                 'persons': {'detected': False}, 'faces': {'detected': False, 'count': 0}}
        adapter.on_vision_frame(frame)
        task = adapter._target_sample_task
        if wait and task is not None:
            await task
        return task

    return SimpleNamespace(adapter=adapter, robot=robot, snapshot=snapshot, calls=calls,
                           records=records, clock=clock, image=image, requests=requests,
                           response=response, add=add, sample=sample)


@pytest.mark.asyncio
async def test_static_frames_use_the_success_based_fallback_instead_of_one_model_call_per_frame(scheduled):
    f = scheduled
    f.add('书本')
    await f.sample(100)
    for now in (101, 102, 103, 110, 129.999):
        await f.sample(now)
    assert [row[0] for row in f.requests] == [100]
    await f.sample(130)
    assert [row[0] for row in f.requests] == [100, 130]
    assert not f.calls


@pytest.mark.asyncio
async def test_changed_frames_remain_pending_until_the_three_second_start_interval(scheduled):
    f = scheduled
    f.add('书本')
    await f.sample(100)
    f.image.content = b'book introduced'
    await f.sample(101)
    await f.sample(102.999)
    assert len(f.requests) == 1
    await f.sample(103)
    assert [row[0] for row in f.requests] == [100, 103]
    await f.sample(104)
    assert len(f.requests) == 2


@pytest.mark.asyncio
async def test_failed_analysis_backs_off_ten_seconds_even_with_new_teaching_and_changed_scene(scheduled):
    f = scheduled
    f.add('书本')
    f.response.error = RuntimeError('simulated model outage')
    await f.sample(100)
    f.add('食物')
    f.image.content = b'changed scene'
    for now in (101, 103, 109.999):
        await f.sample(now)
    assert len(f.requests) == 1
    f.response.error = None
    await f.sample(110)
    assert [row[0] for row in f.requests] == [100, 110]
    assert f.requests[-1][2] == ('书本', '食物')
    await f.sample(111)
    assert len(f.requests) == 2


@pytest.mark.asyncio
async def test_new_teaching_forces_one_batched_check_without_waiting_for_static_fallback(scheduled):
    f = scheduled
    f.add('书本')
    await f.sample(100)
    f.add('食物')
    await f.sample(101)
    assert len(f.requests) == 1
    await f.sample(103)
    assert f.requests == [(100, 1, ('书本',)), (103, 3, ('书本', '食物'))]


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['rest', 'remote', 'busy', 'disconnected', 'no_rules'])
async def test_paused_consumers_do_not_analyze_and_resume_with_a_first_check(scheduled, reason):
    f = scheduled
    record = f.add('书本')
    await f.sample(100)
    if reason == 'rest':
        f.snapshot['activity_state'] = 'rest'
    elif reason == 'remote':
        f.snapshot['control_mode'] = 'remote'
    elif reason == 'busy':
        f.snapshot['pending_action'] = {'command': 'Hello', 'phase': 'accepted'}
    elif reason == 'disconnected':
        f.robot.is_connected = False
    else:
        f.records.clear()
    await f.sample(105)
    await f.sample(110)
    assert len(f.requests) == 1
    f.snapshot.update(activity_state='active', control_mode='normal')
    f.snapshot.pop('pending_action', None)
    f.robot.is_connected = True
    if not f.records:
        f.records.append(record)
    await f.sample(111)
    assert [row[0] for row in f.requests] == [100, 111]


@pytest.mark.asyncio
async def test_a_slow_analysis_owns_the_only_lane_and_uses_its_actual_completion_time(scheduled):
    f = scheduled
    f.add('书本')
    f.response.entered, f.response.finish = asyncio.Event(), asyncio.Event()
    first = await f.sample(100, wait=False)
    await f.response.entered.wait()
    f.image.content = b'changed during analysis'
    await f.sample(103, wait=False)
    await f.sample(110, wait=False)
    assert len(f.requests) == 1
    f.response.finish.set()
    await first
    f.response.entered = None
    await f.sample(111)
    assert len(f.requests) == 2  # The later changed scene was never analyzed.
    await f.sample(140)
    assert len(f.requests) == 2
    await f.sample(141)
    assert len(f.requests) == 3


@pytest.mark.asyncio
async def test_an_executing_single_step_does_not_turn_static_sampling_into_periodic_model_requests(scheduled):
    f = scheduled
    f.add('书本')
    await f.sample(100)
    worker = f.adapter._target_listeners['书本']
    worker._dispatching_task = asyncio.current_task()
    worker._frame_ts = time.time() - 4
    f.snapshot['pending_action'] = {'command': 'Hello', 'phase': 'accepted'}
    try:
        await f.sample(103)
        assert len(f.requests) == 1
    finally:
        worker._dispatching_task = None


def stale_target_context(f):
    from cloud.space.user_goals import get_goal_registry
    worker = f.adapter._target_listeners['书本']
    worker._presence = True
    worker._visible_latched = True
    worker._frame_ts = time.time() - 6
    context = replace(worker._context, trigger_ref='书本', trigger_revision=1,
                      trigger_mode_epoch=worker._generation,
                      goal_revision=get_goal_registry().current()['revision'],
                      expires_at=math.ceil(time.time() + 30), training_run_id='original-run')
    return worker, context


@pytest.mark.asyncio
async def test_local_unchanged_frames_never_renew_expired_target_evidence(scheduled):
    f = scheduled
    f.add('书本')
    await f.sample(100)
    worker, context = stale_target_context(f)
    observed_at = worker._frame_ts
    await f.sample(103)
    assert len(f.requests) == 1
    assert worker._frame_ts == observed_at
    assert not worker.allows(context)


@pytest.mark.asyncio
async def test_original_run_waits_for_new_evidence_despite_an_unrelated_update(scheduled):
    f = scheduled
    f.add('书本')
    await f.sample(100)
    worker, context = stale_target_context(f)
    pending = asyncio.create_task(worker.refresh_observation(context))
    await asyncio.sleep(0)
    assert '书本' in f.adapter._target_refresh
    f.adapter._notify_target_update()  # Other target/state updates are not evidence.
    await asyncio.sleep(0)
    assert not pending.done()
    f.response.detected = True
    await f.sample(103)
    refreshed = await asyncio.wait_for(pending, 1)
    assert refreshed['ts'] == worker._frame_ts
    assert refreshed['frame_id'] == worker._frame_id
    assert refreshed['values'] == {'target': '书本', 'detected': True}
    assert [row[0] for row in f.requests] == [100, 103]
    assert not f.calls
    assert not f.adapter._target_refresh


@pytest.mark.asyncio
async def test_user_takeover_wakes_and_cancels_the_original_evidence_wait(scheduled):
    f = scheduled
    f.add('书本')
    await f.sample(100)
    worker, context = stale_target_context(f)
    pending = asyncio.create_task(worker.refresh_observation(context))
    await asyncio.sleep(0)
    assert '书本' in f.adapter._target_refresh
    f.adapter.interrupt()
    assert await asyncio.wait_for(pending, 1) is None
    assert not f.adapter._target_refresh and not f.calls
