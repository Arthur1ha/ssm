"""Person teachings reuse one graph and command boundary in both Go2 modes."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cloud.agents.common.training.check_fixtures import person_greeting_fixture
from cloud.go2 import training_events as events
from cloud.go2.command_request_checks import device


@pytest.fixture
def listener(monkeypatch, tmp_path):
    f = person_greeting_fixture()
    snapshot = {'connected': True, 'epoch': 'test-connection', 'body_state': 'standing',
                'body_telemetry_fresh': True, 'control_mode': 'normal', 'life_state': 'idle'}
    robot = SimpleNamespace(is_connected=True, _command_lock=asyncio.Lock(),
                            state_snapshot=lambda: dict(snapshot))
    from cloud.go2 import service
    drive = SimpleNamespace(_pending_question_id=None, _user_handoff_in_progress=False,
                            on_vision_frame=MagicMock(), start=MagicMock(), stop=MagicMock())
    monkeypatch.setattr(service, 'drive', drive)
    monkeypatch.setattr(events, 'go2', robot)
    monkeypatch.setattr(events, 'runtime_for', lambda _: None)
    monkeypatch.setattr(events, '_build_go2_card', lambda: {'unit_id': 'go2'})
    monkeypatch.setattr(events, 'catalog_for', lambda _: f['catalog'])
    monkeypatch.setattr(events, 'list_records', lambda *a, **k: f['records'])
    monkeypatch.setattr(events, 'load_runtime', lambda *a: True)
    monkeypatch.setattr(events, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(events, 'get_profile', lambda: {})
    monkeypatch.setattr(events, 'prompt_space_context', lambda: '用户当前在工作，空间有人来访。')
    monkeypatch.setattr(events, 'get_text_llm', lambda: pytest.fail('pure event must not need a model'))
    monkeypatch.setattr(events, 'control_lease', SimpleNamespace(snapshot=lambda: None))
    monkeypatch.setenv('SSM_CHECKPOINT_DIR', str(tmp_path / 'checkpoints'))
    calls = []
    async def execute(name, params, *, context, request_id):
        calls.append((name, params, context, request_id))
        return {'result': 'accepted'}
    monkeypatch.setattr(events, 'execute_tool', execute)
    listener = events.PersonTeachingListener()
    listener.configure(connected=True)
    return listener, robot, snapshot, drive, calls


def frame(index, detected):
    return {'frame_id': index, 'ts': time.time() - .1 + index * .001,
            'persons': {'detected': detected}}


@pytest.fixture
def bound_listener(device, monkeypatch, tmp_path):
    from cloud.agents.common.training import service, store
    robot, lease = device
    f = person_greeting_fixture()
    monkeypatch.setenv('SSM_TRAINING_DIR', str(tmp_path / 'training'))
    monkeypatch.setenv('SSM_CHECKPOINT_DIR', str(tmp_path / 'checkpoints'))
    monkeypatch.setattr(service, '_send_loaded_ack', lambda *a: True)
    monkeypatch.setattr(events, 'go2', robot)
    monkeypatch.setattr(events, 'control_lease', lease)
    monkeypatch.setattr('cloud.go2.control.control_lease', lease)
    monkeypatch.setattr(events, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(events, 'get_profile', lambda: {})
    monkeypatch.setattr(events, 'prompt_space_context', lambda: '')
    adapter = events.PersonTeachingListener()
    monkeypatch.setattr(events, 'person_teachings', adapter)
    monkeypatch.setattr('cloud.go2.service.person_teachings', adapter)
    adapter.configure(connected=True)
    adapter._presence = True
    adapter._frame_ts = time.time()
    with store.partition_lock('go2', space_id='main', write=True) as data:
        data['records'][f['records'][0]['teaching_id']] = f['records'][0]
    return adapter, robot, lease, f


async def flush(listener):
    while listener._task is not None:
        current = listener._task
        try:
            await current
        except asyncio.CancelledError:
            if not current.cancelled():
                raise
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_first_person_frame_triggers_and_continuous_presence_is_deduplicated(listener):
    adapter, _, _, _, calls = listener
    adapter.on_vision_frame(frame(1, True))
    await flush(adapter)
    adapter.on_vision_frame(frame(2, True))
    await flush(adapter)
    assert len(calls) == 1
    assert calls[0][0:2] == ('go2_sport', {'cmd': 'Hello'})
    assert calls[0][2].origin_kind == 'agent_autonomous'
    assert calls[0][2].actor == 'visitor' and not calls[0][2].persist
    adapter.on_vision_frame(frame(3, False))
    adapter.on_vision_frame(frame(4, True))
    await flush(adapter)
    assert len(calls) == 2 and calls[0][3] != calls[1][3]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['manual', 'free_explore'])
async def test_vision_registration_and_hello_do_not_require_free_exploration(listener, monkeypatch, mode):
    adapter, robot, _, drive, calls = listener
    from cloud.go2 import service
    callbacks = []
    loop = SimpleNamespace(add_callback=lambda callback: callbacks.append(callback),
                           remove_callback=lambda callback: callbacks.remove(callback) if callback in callbacks else None,
                           start=MagicMock(), stop=MagicMock())
    monkeypatch.setattr(service, 'go2', robot)
    monkeypatch.setattr(service, 'runtime_for', lambda _: None)
    monkeypatch.setattr(service, 'person_teachings', adapter)
    monkeypatch.setattr(service, 'vision_loop', loop)
    monkeypatch.setattr(service, 'identity_skill', SimpleNamespace(enabled=False))
    for name in ('_active_rule_cb', '_active_drive_cb', '_active_identity_cb'):
        monkeypatch.setattr(service, name, None)
    service._configure_vision_consumers(mode)
    assert callbacks.count(adapter.on_vision_frame) == 1
    assert (drive.on_vision_frame in callbacks) == (mode == 'free_explore')
    adapter.on_vision_frame(frame(1, True))
    await flush(adapter)
    assert len(calls) == 1
    loop.start.assert_called_once()


@pytest.mark.asyncio
async def test_mode_change_keeps_presence_and_invalidates_old_event_context(listener):
    adapter, _, _, _, calls = listener
    before = replace(adapter._context, trigger_mode_epoch=adapter._generation,
                     expires_at=int(time.time() + 5))
    adapter.on_vision_frame(frame(1, True))
    await flush(adapter)
    adapter.configure(connected=True, owner_session_id='exploration-owner')
    assert not adapter.allows(before)
    adapter.on_vision_frame(frame(2, True))
    await flush(adapter)
    assert len(calls) == 1
    adapter.on_vision_frame(frame(3, False))
    adapter.on_vision_frame(frame(4, True))
    await flush(adapter)
    assert len(calls) == 2
    assert calls[-1][2].session_id == 'exploration-owner'


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['user', 'disconnect', 'mode'])
async def test_handoff_after_observation_prevents_pending_dispatch(listener, monkeypatch, reason):
    adapter, robot, _, _, calls = listener
    entered, release = asyncio.Event(), asyncio.Event()
    async def loading(callback, *args):
        entered.set()
        await release.wait()
        return callback(*args)
    monkeypatch.setattr(events, 'invoke_blocking', loading)
    adapter.on_vision_frame(frame(1, True))
    await entered.wait()
    if reason == 'user':
        adapter.interrupt()
    elif reason == 'disconnect':
        robot.is_connected = False
        adapter.configure(connected=False)
    else:
        adapter.configure(connected=True)
    release.set()
    await flush(adapter)
    assert calls == []


@pytest.mark.asyncio
async def test_new_appearance_replaces_pending_judgment_without_losing_the_new_person(listener, monkeypatch):
    adapter, _, _, _, calls = listener
    f = person_greeting_fixture()
    f['records'][0]['definition']['situation'] = '当前适合与人互动时'
    monkeypatch.setattr(events, 'list_records', lambda *a, **k: f['records'])
    entered, release = asyncio.Event(), asyncio.Event()
    decisions = []
    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self
        async def ainvoke(self, _messages):
            decisions.append(len(decisions) + 1)
            if len(decisions) == 1:
                entered.set()
                await release.wait()
            return {'choice': 'accept', 'reason': '当前情境允许互动'}
    monkeypatch.setattr(events, 'get_text_llm', Model)
    adapter.on_vision_frame(frame(1, True))
    await entered.wait()
    adapter.on_vision_frame(frame(2, False))
    adapter.on_vision_frame(frame(3, True))
    await flush(adapter)
    adapter.on_vision_frame(frame(4, True))
    await flush(adapter)
    assert len(decisions) == 2
    assert len(calls) == 1
    assert adapter._pending_appearance is None


@pytest.mark.asyncio
async def test_person_leaving_during_judgment_cannot_be_reported_as_still_seen(listener, monkeypatch):
    adapter, _, _, _, calls = listener
    f = person_greeting_fixture()
    f['records'][0]['definition']['situation'] = '当前适合与人互动时'
    monkeypatch.setattr(events, 'list_records', lambda *a, **k: f['records'])
    entered, release = asyncio.Event(), asyncio.Event()
    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self
        async def ainvoke(self, _messages):
            entered.set()
            await release.wait()
            return {'choice': 'accept', 'reason': '适用'}
    monkeypatch.setattr(events, 'get_text_llm', Model)
    adapter.on_vision_frame(frame(1, True))
    await entered.wait()
    adapter.on_vision_frame(frame(2, False))
    release.set()
    await flush(adapter)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('fresh_frames', [True, False])
async def test_slow_judgment_uses_latest_person_fact_without_expiring_the_appearance(listener, monkeypatch, fresh_frames):
    adapter, _, _, _, calls = listener
    f = person_greeting_fixture()
    f['records'][0]['definition']['situation'] = '用户当前工作时'
    monkeypatch.setattr(events, 'list_records', lambda *a, **k: f['records'])
    wall_time = [time.time()]
    monkeypatch.setattr(events.time, 'time', lambda: wall_time[0])
    entered, release = asyncio.Event(), asyncio.Event()
    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self
        async def ainvoke(self, _messages):
            entered.set()
            await release.wait()
            return {'choice': 'accept', 'reason': '当前用户表达的工作情境适用'}
    monkeypatch.setattr(events, 'get_text_llm', Model)
    adapter.on_vision_frame({'frame_id': 1, 'ts': wall_time[0], 'persons': {'detected': True}})
    await entered.wait()
    original_generation = adapter._generation
    wall_time[0] += 8
    if fresh_frames:
        adapter.on_vision_frame({'frame_id': 2, 'ts': wall_time[0], 'persons': {'detected': True}})
    release.set()
    await flush(adapter)
    assert len(calls) == (1 if fresh_frames else 0)
    assert adapter._generation == original_generation


@pytest.mark.asyncio
async def test_semantic_judgment_still_has_a_bounded_deadline(listener, monkeypatch):
    adapter, _, _, _, calls = listener
    f = person_greeting_fixture()
    f['records'][0]['definition']['situation'] = '当前适合互动的时候'
    monkeypatch.setattr(events, 'list_records', lambda *a, **k: f['records'])
    monkeypatch.setattr(events, 'TEACHING_JUDGMENT_TIMEOUT_S', .01)
    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self
        async def ainvoke(self, _messages):
            await asyncio.Event().wait()
    monkeypatch.setattr(events, 'get_text_llm', Model)
    adapter.on_vision_frame(frame(1, True))
    await asyncio.wait_for(flush(adapter), timeout=2)
    assert calls == []


@pytest.mark.asyncio
async def test_new_appearance_does_not_cancel_or_queue_behind_physical_work(listener, monkeypatch):
    adapter, _, _, _, calls = listener
    entered, release = asyncio.Event(), asyncio.Event()
    async def execute(name, params, *, context, request_id):
        calls.append((name, params, context, request_id))
        entered.set()
        await release.wait()
        return {'result': 'ok', 'confirmation_scope': 'completion'}
    monkeypatch.setattr(events, 'execute_tool', execute)
    adapter.on_vision_frame(frame(1, True))
    await entered.wait()
    physical = adapter._task
    adapter.on_vision_frame(frame(2, False))
    adapter.on_vision_frame(frame(3, True))
    assert not physical.cancelled()
    assert adapter._pending_appearance is None
    release.set()
    await flush(adapter)
    adapter.on_vision_frame(frame(4, True))
    await flush(adapter)
    assert len(calls) == 1 and adapter.last_result['result'] == 'ok'


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', ['accept', 'decline', 'defer'])
async def test_event_situation_is_judged_by_existing_graph_before_execution(listener, monkeypatch, choice):
    adapter, _, _, _, calls = listener
    f = person_greeting_fixture()
    f['records'][0]['definition']['situation'] = '有人来访且主人正在工作时'
    monkeypatch.setattr(events, 'list_records', lambda *a, **k: f['records'])
    messages = []
    class Model:
        def with_structured_output(self, schema, **kwargs):
            return self
        async def ainvoke(self, current):
            messages.extend(current)
            return {'choice': choice, 'reason': '已根据当前用户活动核对情境'}
    monkeypatch.setattr(events, 'get_text_llm', Model)
    adapter.on_vision_frame(frame(1, True))
    await flush(adapter)
    assert len(calls) == (1 if choice == 'accept' else 0)
    assert messages[0].type == 'system' and messages[1].type == 'human'
    assert '用户当前在工作' in messages[1].content
    assert '有人来访且主人正在工作时' in messages[1].content
    if calls:
        assert calls[0][2].trigger_situation_confirmed is True


@pytest.mark.asyncio
async def test_missing_situation_judgment_does_not_execute(listener, monkeypatch):
    adapter, _, _, _, calls = listener
    f = person_greeting_fixture()
    f['records'][0]['definition']['situation'] = '在适合与人互动的时候'
    monkeypatch.setattr(events, 'list_records', lambda *a, **k: f['records'])
    monkeypatch.setattr(events, 'get_text_llm', lambda: None)
    adapter.on_vision_frame(frame(1, True))
    await flush(adapter)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', ['relaxing', 'invented', None])
async def test_multiple_event_situations_choose_at_most_one_existing_teaching(listener, monkeypatch, selected):
    adapter, _, _, _, calls = listener
    f = person_greeting_fixture()
    working = f['records'][0]
    working['teaching_id'] = 'working'
    working['definition']['situation'] = '我在工作的时候'
    relaxing = deepcopy(working)
    relaxing['teaching_id'] = 'relaxing'
    relaxing['definition']['situation'] = '我在休息的时候'
    monkeypatch.setattr(events, 'list_records', lambda *a, **k: [working, relaxing])
    monkeypatch.setattr(events, 'prompt_space_context', lambda: '用户说：我现在休息一下。')
    class Model:
        def with_structured_output(self, schema, **kwargs):
            return self
        async def ainvoke(self, messages):
            assert '我现在休息一下' in messages[1].content
            assert 'working' in messages[1].content and 'relaxing' in messages[1].content
            return {'choice': 'accept', 'reason': '按当前用户情境选择', 'teaching_id': selected}
    monkeypatch.setattr(events, 'get_text_llm', Model)
    adapter.on_vision_frame(frame(1, True))
    await flush(adapter)
    if selected == 'relaxing':
        assert len(calls) == 1
        assert calls[0][2].trigger_ref == 'relaxing'
        assert calls[0][2].trigger_situation_confirmed is True
    else:
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'body_state': 'lying'}, {'body_telemetry_fresh': False},
    {'life_state': 'play'}, {'control_mode': 'remote'}, {'navigation_active': True},
])
async def test_real_device_ineligibility_cannot_be_bypassed_by_mode(listener, changes):
    adapter, _, snapshot, _, calls = listener
    snapshot.update(changes)
    adapter.on_vision_frame(frame(1, True))
    await flush(adapter)
    assert calls == []


@pytest.mark.asyncio
async def test_stale_unknown_and_replayed_frames_do_not_create_appearances(listener):
    adapter, _, _, _, calls = listener
    adapter.on_vision_frame({'frame_id': 1, 'ts': time.time() - 6,
                             'persons': {'detected': True}})
    adapter.on_vision_frame(frame(2, None))
    valid = frame(3, True)
    adapter.on_vision_frame(valid)
    await flush(adapter)
    adapter.on_vision_frame({**valid, 'persons': {'detected': False}})
    adapter.on_vision_frame(frame(4, True))
    await flush(adapter)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_another_users_control_lease_does_not_become_teaching_authority(listener, monkeypatch):
    adapter, _, _, _, calls = listener
    monkeypatch.setattr(events, 'control_lease', SimpleNamespace(
        snapshot=lambda: SimpleNamespace(owner_session_id='user-operating-robot')))
    adapter.on_vision_frame(frame(1, True))
    await flush(adapter)
    assert calls == []


@pytest.mark.asyncio
async def test_bound_event_reuses_command_ledger_and_manual_hello_stays_available(bound_listener):
    from cloud.go2.tools.executor import execute_tool
    adapter, robot, _, f = bound_listener
    robot.send_command.return_value = {'result': 'ok', 'confirmation_scope': 'completion'}
    now = time.time()
    context = replace(adapter._context, trigger_mode_epoch=adapter._generation,
                      expires_at=int(now + 5))
    event = {'event_id': 'frame:deduplicated', 'kind': 'training_event',
             'ts': now, 'deadline': now + 5}
    await adapter.handle_event(event, context)
    await adapter.handle_event(event, context)
    assert robot.send_command.await_count == 1
    assert adapter.last_result['replayed'] is True
    manual = replace(context, actor='admin', persist=True, origin_kind='user_delegated',
                     trigger_mode_epoch=None)
    assert (await execute_tool('go2_sport', {'cmd': 'Hello'}, context=manual))['result'] == 'ok'
    assert robot.send_command.await_count == 2
    stale = replace(context, trigger_ref=f['records'][0]['teaching_id'],
                    trigger_revision=f['records'][0]['revision'],
                    trigger_transition_id=f['records'][0]['definition']['transition_ids'][0])
    blocked = await execute_tool('go2_sport', {'cmd': 'Hello'}, context=stale, request_id='stale-event')
    assert blocked['result'] == 'blocked'
    assert blocked['reasons'][0]['code'] == 'autonomy_unavailable'
    assert robot.send_command.await_count == 2


@pytest.mark.asyncio
async def test_completed_automatic_hello_does_not_hold_a_visitors_control_lease(bound_listener):
    from cloud.go2.tools.executor import execute_tool
    from cloud.space.execution import ExecutionContext
    adapter, robot, lease, _ = bound_listener
    robot.send_command.return_value = {'result': 'ok', 'confirmation_scope': 'completion'}
    now = time.time()
    context = replace(adapter._context, trigger_mode_epoch=adapter._generation,
                      expires_at=int(now + 30))
    await adapter.handle_event({'event_id': 'completed', 'kind': 'training_event',
                                'ts': now, 'deadline': now + 30}, context)
    assert adapter.last_result['result'] == 'ok' and lease.snapshot() is None
    visitor = ExecutionContext('new-pwa-visitor', 'visitor', False, expires_at=int(now + 30))
    assert (await execute_tool('go2_sport', {'cmd': 'Hello'}, context=visitor))['result'] == 'ok'
    assert robot.send_command.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('initial', ['accepted', 'timeout'])
@pytest.mark.parametrize('newer_lease', [False, True])
async def test_late_completion_releases_only_the_automatic_actions_own_lease(bound_listener, initial, newer_lease):
    from cloud.go2.tasks import observe_command_results
    adapter, robot, lease, _ = bound_listener
    robot.send_command.return_value = {'result': initial, 'request_id': 'late-physical',
                                       'confirmation_scope': 'request'}
    now = time.time()
    context = replace(adapter._context, trigger_mode_epoch=adapter._generation,
                      expires_at=int(now + 30))
    await adapter.handle_event({'event_id': 'pending', 'kind': 'training_event',
                                'ts': now, 'deadline': now + 30}, context)
    held = lease.snapshot()
    assert held is not None and held.owner_session_id == 'go2-teachings'
    if newer_lease:
        newer = lease.acquire(adapter._context)
        assert newer.generation > held.generation
    robot.runtime.commit('real_completion', last_action={
        'request_id': 'late-physical', 'result': 'ok', 'confirmation': 'joint_posture'})
    observe_command_results(robot)
    if newer_lease:
        assert lease.snapshot().generation == newer.generation
    else:
        assert lease.snapshot() is None
