"""Rule-selected visual targets and trusted stranger observations, without hardware."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud.agents.common.training.policy import conflicts, validate_definition
from cloud.go2 import training_events as events
from cloud.go2.command_request_checks import device
from cloud.go2.training_event_checks import listener, bound_listener, flush, face_frame


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    for key in ('SSM_USER_GOALS_DIR', 'SSM_TRAINING_DIR', 'SSM_EFFECT_LEDGER_DIR',
                'SSM_CHECKPOINT_DIR', 'SSM_RUN_INDEX_DIR'):
        monkeypatch.setenv(key, str(tmp_path / key))


@pytest.fixture
def dynamic(listener, monkeypatch):
    adapter, robot, snapshot, drive, calls = listener
    catalog = events.catalog_for({})
    records = events.list_records('go2')
    template = deepcopy(records[0])
    records.clear()

    def add(target, *, teaching_id=None):
        record = deepcopy(template)
        record['teaching_id'] = teaching_id or target
        record['name'] = '看到' + target + '就挥手'
        record['definition']['trigger'] = {
            'event_id': 'vision.target_presence', 'where': {'target': target, 'detected': True}}
        records.append(record)
        return record

    observations = {}
    requests = []

    async def observe(jpeg, frame, targets):
        requests.append((jpeg, frame, targets))
        return {target: observations.get(target) for target in targets}

    from cloud.go2.perception import teaching_targets, vision
    class EverySuppliedObservation:
        # Source/appearance tests supply semantic observations explicitly.
        # Real image admission and timing have independent integration tests.
        def observe(self, _jpeg, **_kwargs):
            return SimpleNamespace(changed=True, initial=True, token=None)
        def mark_analyzed(self, _token):
            return True

    monkeypatch.setattr(events, 'SceneChangeGate', EverySuppliedObservation)
    adapter._target_scene = EverySuppliedObservation()
    adapter._target_schedule.min_interval_s = 0
    monkeypatch.setattr(teaching_targets, 'observe_targets', observe)
    monkeypatch.setattr(vision.vision_loop, 'get_frame', lambda frame_id: b'frame-' + str(frame_id).encode())

    async def sample(index):
        frame = {'frame_id': index, 'source_frame_id': index, 'camera_session_id': 'camera-1',
                 'ts': time.time() - .01, 'persons': {'detected': False},
                 'faces': {'detected': False, 'count': 0}}
        adapter.on_vision_frame(frame)
        task = adapter._target_sample_task
        if task is not None:
            await task
        for worker in adapter._workers():
            await flush(worker)
        return frame

    return SimpleNamespace(adapter=adapter, robot=robot, snapshot=snapshot, drive=drive,
                           calls=calls, records=records, catalog=catalog, add=add,
                           observations=observations, requests=requests, sample=sample)


def test_target_parameters_are_required_and_different_targets_do_not_conflict(dynamic):
    book, food = dynamic.add('书本'), dynamic.add('食物')
    assert not conflicts(book['definition'], [food])
    assert conflicts(book['definition'], [deepcopy(book)])[0]['code'] == 'event_overlap'
    for where in ({}, {'target': '书本'}, {'target': '', 'detected': True},
                  {'target': '书本', 'detected': 1}, {'target': '书本', 'detected': False},
                  {'target': '书本', 'detected': True, 'unknown': True}):
        value = deepcopy(book['definition'])
        value['trigger']['where'] = where
        with pytest.raises(ValueError):
            validate_definition(value, dynamic.catalog)


def test_target_observation_cannot_add_an_unknown_or_untrainable_action(dynamic):
    record = dynamic.add('书本')
    disabled = next(edge['id'] for edge in dynamic.catalog['transitions'] if not edge['trainable'])
    for transition_id in ('tr_invented_backflip', disabled):
        value = deepcopy(record['definition'])
        value['steps'][0]['transition_ids'] = [transition_id]
        with pytest.raises(ValueError, match='unknown or untrainable transition'):
            validate_definition(value, dynamic.catalog)


@pytest.mark.asyncio
async def test_only_confirmed_demand_is_observed_and_targets_are_batched(dynamic):
    f = dynamic
    await f.sample(1)
    assert not f.requests and not f.calls
    f.add('书本')
    f.add('食物')
    f.observations.update({'书本': True, '食物': False})
    await f.sample(2)
    assert len(f.requests) == 1 and f.requests[0][2] == ['书本', '食物']
    assert len(f.calls) == 1
    assert f.calls[0][2].trigger_observation_id == 'vision.target_presence'
    assert f.calls[0][2].trigger_ref == '书本'
    assert f.calls[0][2].goal_revision > 0


@pytest.mark.asyncio
async def test_visible_target_is_deduplicated_uncertainty_does_not_rearm_and_absence_does(dynamic):
    f = dynamic
    f.add('书本')
    f.observations['书本'] = True
    await f.sample(1)
    await f.sample(2)
    f.observations['书本'] = None
    await f.sample(3)
    f.observations['书本'] = True
    await f.sample(4)
    assert len(f.calls) == 1
    f.observations['书本'] = False
    await f.sample(5)
    f.observations['书本'] = True
    await f.sample(6)
    assert len(f.calls) == 2 and f.calls[0][3] != f.calls[1][3]


@pytest.mark.asyncio
async def test_rule_storage_failure_revokes_facts_without_rearming_visible_target(dynamic, monkeypatch):
    f = dynamic
    f.add('书本')
    f.observations['书本'] = True
    await f.sample(1)
    read = f.adapter._read_target_signatures

    def failed():
        raise ValueError('training_storage_unreadable')

    monkeypatch.setattr(f.adapter, '_read_target_signatures', failed)
    await f.sample(2)
    assert not f.adapter.allows(f.calls[0][2])
    monkeypatch.setattr(f.adapter, '_read_target_signatures', read)
    await f.sample(3)
    assert len(f.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['remove', 'revision', 'takeover', 'reconnect', 'new_goal'])
async def test_late_target_observation_cannot_dispatch_after_revocation(dynamic, monkeypatch, change):
    f = dynamic
    record = f.add('书本')
    from cloud.go2.perception import teaching_targets
    entered, finish = asyncio.Event(), asyncio.Event()

    async def observe(*_args):
        entered.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            # A provider may finish despite cancellation; generation must still win.
            await finish.wait()
        return {'书本': True}

    monkeypatch.setattr(teaching_targets, 'observe_targets', observe)
    sampling = asyncio.create_task(f.sample(1))
    await entered.wait()
    if change == 'remove':
        f.records.clear()
    elif change == 'revision':
        record['revision'] += 1
    elif change == 'takeover':
        f.adapter.interrupt()
    elif change == 'new_goal':
        from cloud.space.user_goals import get_goal_registry
        get_goal_registry().advance('user', 'replacement-goal')
    else:
        f.snapshot['epoch'] = 'replacement-connection'
        f.adapter.configure(connected=True)
    finish.set()
    await sampling
    assert not f.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('read_fails', [False, True])
async def test_takeover_during_final_rule_read_discards_result_and_late_failure(dynamic, monkeypatch, read_fails):
    f = dynamic
    f.add('书本')
    f.observations['书本'] = True
    entered, finish = asyncio.Event(), asyncio.Event()
    original = events.invoke_blocking
    read_count = 0

    async def controlled(callback, *args, **kwargs):
        nonlocal read_count
        if callback == f.adapter._read_target_signatures:
            read_count += 1
            if read_count == 2:
                entered.set()
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    await finish.wait()
                if read_fails:
                    raise ValueError('late_old_store_failure')
        return await original(callback, *args, **kwargs)

    monkeypatch.setattr(events, 'invoke_blocking', controlled)
    sampling = asyncio.create_task(f.sample(1))
    await entered.wait()
    f.adapter.interrupt()
    worker = f.adapter._target_listeners['书本']
    worker._presence = True  # New-generation fact must survive an old read failure.
    finish.set()
    await sampling
    assert not f.calls and worker._presence is True


@pytest.mark.asyncio
@pytest.mark.parametrize('visible', [True, False, None])
async def test_target_fact_refreshes_while_its_original_behavior_is_running(dynamic, monkeypatch, visible):
    f = dynamic
    f.add('书本')
    f.observations['书本'] = True
    from cloud.go2 import service
    entered, finish = asyncio.Event(), asyncio.Event()
    checked = []

    async def behavior(prepared, context, *, run_id, availability):
        entered.set()
        await finish.wait()
        checked.append(availability(replace(context, training_run_id=run_id)))
        return {'result': 'ok'}

    monkeypatch.setattr(service, 'run_training_behavior', behavior)
    first = asyncio.create_task(f.sample(1))
    await entered.wait()
    worker = f.adapter._target_listeners['书本']
    previous_ts = worker._frame_ts
    # A real pending action blocks new actions but must not stop camera facts.
    f.snapshot['pending_action'] = {'command': 'Hello', 'phase': 'accepted'}
    # A person leaving is independent of a still-visible rule target.
    f.adapter._presence = True
    f.observations['书本'] = visible
    frame = {'frame_id': 2, 'source_frame_id': 2, 'camera_session_id': 'camera-1',
             'ts': time.time(), 'persons': {'detected': False}, 'faces': {'detected': False, 'count': 0}}
    f.adapter.on_vision_frame(frame)
    task = f.adapter._target_sample_task
    if task:
        await task
    assert worker._frame_ts > previous_ts and worker._presence is visible
    f.snapshot.pop('pending_action')
    finish.set()
    await first
    assert checked == [visible is True]


@pytest.mark.asyncio
async def test_new_user_goal_discards_waiting_appearance_without_rebinding(listener):
    from cloud.space.user_goals import get_goal_registry
    adapter, _robot, snapshot, _drive, calls = listener
    snapshot['body_telemetry_fresh'] = False
    adapter.on_vision_frame({'frame_id': 1, 'ts': time.time() - .01, 'persons': {'detected': True}})
    assert adapter._waiting_appearance is not None
    get_goal_registry().advance('user', 'new-target')
    snapshot['body_telemetry_fresh'] = True
    adapter.on_vision_frame({'frame_id': 2, 'ts': time.time(), 'persons': {'detected': True}})
    await flush(adapter)
    assert not calls and adapter._waiting_appearance is None


@pytest.mark.asyncio
async def test_late_identity_binds_its_source_frame_goal_not_the_latest_frame(listener):
    from cloud.space.user_goals import get_goal_registry
    adapter, _robot, _snapshot, _drive, calls = listener
    record = events.list_records('go2')[0]
    record['definition']['trigger'] = {'event_id': 'vision.owner_recognized', 'where': {'recognized': True}}
    original = face_frame(1)
    adapter.on_vision_frame(original)
    get_goal_registry().advance('user', 'new-goal-during-identity')
    adapter.on_vision_frame(face_frame(2))
    person = SimpleNamespace(status='recognized', role='owner', person_id='owner-1',
                             name='主人', score=99, ts=original['ts'], frame_id=1)
    adapter.on_identity_event(SimpleNamespace(current=person))
    await flush(adapter._owner_listener)
    assert not calls


@pytest.mark.asyncio
async def test_stale_visual_response_does_not_renew_capture_time(dynamic, monkeypatch):
    f = dynamic
    f.add('书本')
    from cloud.go2.perception import teaching_targets
    wall = [time.time()]
    monkeypatch.setattr(events.time, 'time', lambda: wall[0])

    async def slow(*_args):
        wall[0] += 6
        return {'书本': True}

    monkeypatch.setattr(teaching_targets, 'observe_targets', slow)
    await f.sample(1)
    assert not f.calls


@pytest.mark.asyncio
async def test_stranger_requires_a_fresh_successful_unmatched_identity(listener):
    adapter, _robot, _snapshot, _drive, calls = listener
    record = events.list_records('go2')[0]
    record['definition']['trigger'] = {
        'event_id': 'vision.stranger_recognized', 'where': {'recognized': True}}
    adapter.on_vision_frame(face_frame(1))
    for status, role, error in (('error', 'stranger', 'service_unavailable'),
                                ('disabled', 'stranger', None), ('idle', '', None),
                                ('unknown', 'stranger', 'recognition_failed')):
        adapter.on_identity_event(SimpleNamespace(current=SimpleNamespace(
            status=status, role=role, person_id='', error_code=error, ts=time.time())))
        await flush(adapter._stranger_listener)
    assert not calls
    person = SimpleNamespace(status='unknown', role='stranger', person_id='', name='陌生人',
                             score=0, error_code=None, ts=time.time(), frame_id=1)
    adapter.on_identity_event(SimpleNamespace(current=person))
    await flush(adapter._stranger_listener)
    assert len(calls) == 1
    adapter.on_identity_event(SimpleNamespace(current=person))
    await flush(adapter._stranger_listener)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unmatched_provider_result_reaches_original_stranger_command_boundary(bound_listener, monkeypatch):
    from cloud.agents.common.training import store
    from cloud.go2 import service
    from cloud.go2.perception.identity import IdentitySkill
    from cloud.space.people.provider import FaceSearchResult

    adapter, robot, lease, f = bound_listener
    with store.partition_lock('go2', space_id='main', write=True) as data:
        record = data['records'][f['records'][0]['teaching_id']]
        record['definition']['trigger'] = {
            'event_id': 'vision.stranger_recognized', 'where': {'recognized': True}}
    robot.send_command.return_value = {'result': 'ok', 'confirmation_scope': 'completion'}
    source = {**face_frame(1), 'faces': {'detected': True, 'count': 1, 'boxes': [(0, 0, 100, 100)]}}
    provider = SimpleNamespace(search_persons=AsyncMock(return_value=FaceSearchResult(())))
    skill = IdentitySkill(provider, SimpleNamespace(get=lambda _: None), lambda: (source, b'jpeg'))
    monkeypatch.setattr(skill, '_get_face_crop', lambda: b'fresh-face')
    skill.add_callback(service._on_identity_event)
    adapter.on_vision_frame(source)
    result = await skill.recognize_now()
    for worker in adapter._workers():
        await flush(worker)
    assert result.status == 'unknown' and result.role == 'stranger'
    robot.send_command.assert_awaited_once_with('Hello')
    assert adapter._stranger_listener.last_result['result'] == 'ok'
    assert lease.snapshot() is None


@pytest.mark.asyncio
async def test_target_model_output_is_strict_and_preserves_unknown(monkeypatch):
    from cloud.go2.perception import teaching_targets
    monkeypatch.setattr(teaching_targets, '_dimensions', lambda _: (640, 480))
    model = AsyncMock(return_value={'targets': [
        {'index': 0, 'detected': True}, {'index': 1, 'detected': None}]})
    monkeypatch.setattr(teaching_targets, '_ask_vision', model)
    assert await teaching_targets.observe_targets(b'jpeg', {'ts': 123}, ['书本', '食物']) == {
        '书本': True, '食物': None}
    model.return_value = {'targets': [{'index': 0, 'detected': 1}]}
    with pytest.raises(ValueError, match='invalid_teaching_target_observation'):
        await teaching_targets.observe_targets(b'jpeg', {'ts': 123}, ['书本'])
