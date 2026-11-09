"""C/D safety, lifecycle and ownership checks; all hardware is simulated."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.go2.connection.webrtc import Go2Connection
from cloud.go2.connection.fsm import CommandBlocked
from cloud.go2.state import Go2State
from cloud.go2.control import ControlLease
from cloud.space.execution import ExecutionContext


def robot_at(body='standing'):
    robot = Go2Connection()
    robot.is_connected = True
    robot.runtime.body(body, confirmation='observed')
    robot._sport_state_observed_at = time.monotonic()
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(pub_sub=SimpleNamespace(
        publish_request_new=AsyncMock(return_value={'data': {'header': {'status': {'code': 0}}}}),
        publish_without_callback=MagicMock())))
    return robot


def test_connecting_epoch_and_initial_telemetry_do_not_guess_standing():
    robot = Go2Connection()
    first = robot.state_snapshot()
    robot.runtime.connection('connecting')
    robot.is_connected = True
    for _ in range(4):
        robot._on_state({'data': {'body_height': .32, 'mode': 0, 'velocity': [0, 0, 0]}})
    state = robot.state_snapshot()
    assert state['epoch'] != first['epoch']
    assert (state['connection_state'], state['activity_state'], state['life_state']) == ('online', 'active', 'idle')
    assert state['body_state'] is None
    assert state['available_actions'] == ['StopMove']


@pytest.mark.asyncio
async def test_remote_zero_movement_and_exit_have_separate_facts():
    robot = robot_at()
    await robot.send_command('EnterMoveControl')
    assert robot.state_snapshot()['control_mode'] == 'remote'
    assert robot.body_state == 'standing'
    robot.move_velocity(.1, 0, 0)
    assert robot.body_state == 'standing'  # Sending velocity is not observation.
    robot._on_state({'data': {'velocity': [.1, 0, 0]}})
    assert robot.body_state == 'moving'
    robot.move_velocity(0, 0, 0)
    assert robot.state_snapshot()['control_mode'] == 'remote'
    assert robot.body_state is None  # Need physical stopped evidence.
    for _ in range(3):
        robot._on_state({'data': {'velocity': [0, 0, 0]}})
    assert robot.body_state == 'standing'
    await robot.send_command('StopMove')
    assert robot.state_snapshot()['control_mode'] == 'normal'


@pytest.mark.parametrize('body', ['lying', 'sitting'])
def test_emergency_zero_preserves_stable_body_in_rest(body):
    robot = robot_at(body)
    robot.runtime.request_rest()
    robot.stop_velocity()
    assert robot.body_state == body
    assert robot.state_snapshot()['activity_state'] == 'rest'
    with pytest.raises(CommandBlocked):
        robot.move_velocity(.1, 0, 0)


def test_old_life_completion_cannot_end_new_task_or_new_epoch():
    state = Go2State()
    state.connection('online')
    old = state.begin_life('play', 'session', 'first')
    new = state.begin_life('play', 'session', 'second')
    assert state.finish_life(old) is False
    assert state.snapshot()['life_task_id'] == 'second'
    state.connection('offline')
    state.connection('connecting')
    state.connection('online')
    assert state.finish_life(new) is False
    assert state.snapshot()['life_state'] == 'idle'


def test_stale_body_blocks_motion_and_rest_without_lying_about_posture():
    robot = robot_at('standing')
    before = robot.state_snapshot()
    robot._sport_state_observed_at = time.monotonic() - 3
    after = robot.state_snapshot()
    assert after['revision'] > before['revision']
    assert after['body_state'] == 'standing'
    assert after['available_actions'] == ['StopMove']
    assert after['life_blocked_reasons']['request_rest'] == 'body_telemetry_stale'
    with pytest.raises(CommandBlocked, match='stale'):
        robot.move_velocity(.1, 0, 0)


@pytest.mark.asyncio
async def test_old_connection_sensor_callbacks_are_ignored(monkeypatch):
    import cloud.go2.connection.webrtc as module
    callbacks = []
    def factory(*args, **kwargs):
        subscribed = {}
        callbacks.append(subscribed)
        return SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
            datachannel=SimpleNamespace(set_decoder=MagicMock(), disableTrafficSaving=AsyncMock(),
                pub_sub=SimpleNamespace(subscribe=lambda topic, callback: subscribed.__setitem__(topic, callback))),
            pc=SimpleNamespace(getTransceivers=lambda: [], close=AsyncMock()),
            video=SimpleNamespace(switchVideoChannel=MagicMock()))
    monkeypatch.setattr(module, 'UnitreeWebRTCConnection', factory)
    robot = Go2Connection()
    robot.balance_stand = AsyncMock()
    try:
        await robot.connect('test', 'test', 'test')
        old = callbacks[-1][module.RTC_TOPIC['LF_SPORT_MOD_STATE']]
        await robot.disconnect()
        await robot.connect('test', 'test', 'test')
        before = robot.state_snapshot()
        old({'data': {'velocity': [.2, 0, 0], 'body_height': .32}})
        assert robot.state_snapshot() == before
        assert robot._robot_state == {}
        robot.balance_stand.assert_not_awaited()
    finally:
        await robot.disconnect()


@pytest.mark.asyncio
async def test_rejected_navigation_does_not_leave_running_orphan(monkeypatch):
    import importlib
    module = importlib.import_module('cloud.go2.navigation.navigator')
    robot = robot_at('lying')
    robot.runtime.request_rest()
    monkeypatch.setattr(module, 'go2', robot)
    navigator = module.Navigator()
    run = AsyncMock()
    monkeypatch.setattr(navigator, '_run_to', run)
    with pytest.raises(ValueError):
        navigator.start_go_to('home', 'test')
    await asyncio.sleep(0)
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_rest_blocks_queued_autonomous_event_before_graph(monkeypatch):
    import importlib
    module = importlib.import_module('cloud.go2.mode.free_explore')
    robot = robot_at('lying')
    robot.runtime.request_rest()
    monkeypatch.setattr(module, 'go2', robot)
    drive = module.Drive()
    decision = AsyncMock()
    monkeypatch.setattr(drive, '_decide_event', decision)
    messenger = MagicMock()
    await drive._handle_event({'kind': 'peer_message', 'message_id': 'x', 'messenger': messenger,
                               'deadline': time.time() + 60}, None, None, 'test')
    decision.assert_not_awaited()
    messenger.send_result.assert_called_once()


def test_snapshot_uses_one_committed_freshness_fact(monkeypatch):
    import cloud.go2.connection.fsm as module
    robot = robot_at()
    # Crossing a clock boundary midway through projection cannot mix revision
    # and gate fields. Refresh once before the projection and derive from facts.
    calls = 0
    original = robot._refresh_body_freshness
    def refresh():
        nonlocal calls
        calls += 1
        original()
    monkeypatch.setattr(robot, '_refresh_body_freshness', refresh)
    snapshot = robot.state_snapshot()
    assert calls == 1
    assert snapshot['body_telemetry_fresh'] is True
    assert 'Hello' in snapshot['available_actions']


def graph_runtime(tmp_path, monkeypatch, model):
    from cloud.agents.go2 import graph
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.agents.common.run_index import FileRunIndex
    from cloud.agents.go2.run_view import Go2RunView
    from cloud.agents.go2.memory.episode import EpisodeMemory
    from langchain_core.messages import HumanMessage
    robot = robot_at()
    monkeypatch.setenv('SSM_CHECKPOINT_DIR', str(tmp_path / 'checkpoints'))
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    view = Go2RunView(checkpointer=FileCheckpointSaver(tmp_path / 'checkpoints'),
                      index=FileRunIndex(tmp_path / 'index'))
    monkeypatch.setattr(graph, 'go2', robot)
    monkeypatch.setattr(graph, 'get_store', lambda: view)
    monkeypatch.setattr(graph, 'get_text_llm', lambda: model)
    monkeypatch.setattr(graph, 'episode_memory', EpisodeMemory(tmp_path / 'episodes'))
    monkeypatch.setattr(graph, 'recall_memory', lambda *_a, **_k: SimpleNamespace(fragments=[], text=''))
    monkeypatch.setattr(graph, 'go2_messages', lambda *_a, **_k: [HumanMessage(content='请与我聊一会儿')])
    monkeypatch.setattr(graph, 'emit_agent_event', lambda *_a, **_k: None)
    monkeypatch.setattr('cloud.agents.go2.personality.profile.get_profile', lambda: {'prompt': 'Go2', 'revision': 1})
    monkeypatch.setattr('cloud.agents.common.runner.emit_agent_event', lambda *_a, **_k: None)
    monkeypatch.setattr('cloud.api.intents.publish_agent_event', lambda *_a, **_k: {})
    context = ExecutionContext('graph-life', 'visitor', False, expires_at=int(time.time()) + 60)
    return robot, graph, context


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_real_graph_owns_play_while_waiting_and_releases_on_terminal(tmp_path, monkeypatch, cancel):
    from langchain_core.messages import AIMessage
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Model(JsonReplyModel):
        def bind_tools(self, *_a, **_k):
            return self
        async def ainvoke(self, _messages):
            entered.set()
            await proceed.wait()
            return AIMessage(content='我在这里。')
    robot, graph, context = graph_runtime(tmp_path, monkeypatch, Model())
    task = asyncio.create_task(graph.run_go2_loop(context.session_id, '聊一会', context=context,
                                                run_id='life-wait', publish_reply=False))
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert robot.state_snapshot()['life_state'] == 'play'
    assert robot.state_snapshot()['life_task_id'] == 'life-wait'
    with pytest.raises(ValueError, match='忙碌'):
        robot.runtime.request_rest()
    if cancel:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    else:
        proceed.set()
        await task
    assert robot.state_snapshot()['life_state'] == 'idle'
    assert robot.state_snapshot()['life_task_id'] is None


@pytest.mark.asyncio
async def test_clarification_releases_play_and_resume_reacquires_same_run(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Model(JsonReplyModel):
        resumed = False
        def bind_tools(self, *_a, **_k):
            return self
        async def ainvoke(self, _messages):
            if not self.resumed:
                return AIMessage(content='{"kind":"question","text":"你想聊什么？"}')
            entered.set()
            await proceed.wait()
            return AIMessage(content='好呀。')
    model = Model()
    robot, graph, context = graph_runtime(tmp_path, monkeypatch, model)
    result = await graph.run_go2_loop(context.session_id, '聊一会', context=context,
                                      run_id='life-clarify', publish_reply=False)
    assert result['result'] == 'needs_clarification'
    assert robot.state_snapshot()['life_state'] == 'idle'
    model.resumed = True
    task = asyncio.create_task(graph.run_go2_loop(context.session_id, '聊今天', context=context,
                              run_id='life-clarify', resume=True, publish_reply=False))
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert robot.state_snapshot()['life_state'] == 'play'
    assert robot.state_snapshot()['life_task_id'] == 'life-clarify'
    proceed.set()
    await task
    assert robot.state_snapshot()['life_state'] == 'idle'


@pytest.mark.asyncio
async def test_explore_handoff_waits_old_navigation_cleanup_and_fresh_stop(monkeypatch):
    import importlib
    router = importlib.import_module('cloud.go2.service')
    nav_module = importlib.import_module('cloud.go2.navigation.navigator')
    robot = robot_at()
    owner = 'handoff-user'
    token = robot.runtime.begin_life('explore', owner, 'explore-old')
    robot.runtime.exploration_enabled = True
    # Move has been sent, but movement telemetry has not arrived yet.
    robot._velocity_command_active = True
    robot._motion_observation_enabled = True
    started, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def old_navigation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()  # Old task's delayed stop must finish first.
            robot.move_velocity(0, 0, 0)
    old = asyncio.create_task(old_navigation())
    await started.wait()
    navigator = SimpleNamespace(_task=old, owner_session_id=owner,
                                stop=lambda **kwargs: old.cancel())
    def stop_drive(**kwargs):
        robot.runtime.finish_life(token)
        return True
    drive = SimpleNamespace(_task=None, owner_session_id=owner, stop=stop_drive)
    monkeypatch.setattr(router, 'go2', robot)
    monkeypatch.setattr(router, 'drive', drive)
    monkeypatch.setattr(nav_module, 'navigator', navigator)
    context = ExecutionContext(owner, 'visitor', False)
    handoff = asyncio.create_task(router.prepare_user_interaction(context))
    await cleanup.wait()
    assert not handoff.done()
    robot._conn.datachannel.pub_sub.publish_request_new.assert_not_awaited()
    release.set()
    await asyncio.sleep(.02)
    assert old.done()
    assert not handoff.done()  # Cancellation alone is not a physical stop.
    for _ in range(3):
        robot._on_state({'data': {'velocity': [0, 0, 0]}})
    await asyncio.wait_for(handoff, timeout=1)
    assert robot.runtime.data['life_state'] == 'idle'
    assert robot.body_state == 'standing'
    robot._conn.datachannel.pub_sub.publish_request_new.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_session_cannot_handoff_exploration(monkeypatch):
    import importlib
    router = importlib.import_module('cloud.go2.service')
    robot = robot_at()
    robot.runtime.begin_life('explore', 'original-owner', 'explore-old')
    drive = SimpleNamespace(stop=MagicMock())
    monkeypatch.setattr(router, 'go2', robot)
    monkeypatch.setattr(router, 'drive', drive)
    with pytest.raises(ValueError, match='another_session'):
        await router.prepare_user_interaction(ExecutionContext('intruder', 'visitor', False))
    drive.stop.assert_not_called()
    assert robot.runtime.data['life_task_id'] == 'explore-old'


@pytest.mark.asyncio
async def test_start_exploration_requires_navigation_before_lease_or_mode_change(monkeypatch):
    import importlib
    router = importlib.import_module('cloud.go2.service')
    nav_module = importlib.import_module('cloud.go2.navigation.navigator')
    robot = robot_at()
    lease = ControlLease()
    configure = MagicMock()
    monkeypatch.setattr(router, 'go2', robot)
    monkeypatch.setattr(router, 'control_lease', lease)
    monkeypatch.setattr(router, '_configure_vision_consumers', configure)
    monkeypatch.setattr(nav_module, 'navigator', SimpleNamespace(mode=SimpleNamespace(value='idle'),
                        _snapshot=lambda: (None, 'map_unavailable')))
    admin = ExecutionContext('admin-start', 'admin', True)
    before = robot.state_snapshot()
    with pytest.raises(ValueError, match='map_unavailable'):
        await router.perform_life_control('start_explore', admin)
    assert robot.state_snapshot() == before
    assert lease.snapshot() is None
    configure.assert_not_called()


@pytest.mark.parametrize('invalid', [{}, {'progress': 0}, {'body_height': float('nan')},
                                   {'velocity': [0, float('nan'), 0]}, {'velocity': [0, 0]}])
def test_invalid_body_frame_does_not_refresh_stale_pose(invalid):
    robot = robot_at()
    robot._sport_state_observed_at = time.monotonic() - 3
    before = robot.state_snapshot()
    robot._on_state({'data': invalid})
    after = robot.state_snapshot()
    assert after['body_telemetry_fresh'] is False
    assert after['available_actions'] == ['StopMove']
    assert after['revision'] == before['revision']


def test_exploration_intent_changes_gate_revision_even_while_idle():
    robot = robot_at()
    before = robot.state_snapshot()
    robot.runtime.exploration_enabled = True
    enabled = robot.state_snapshot()
    assert enabled['revision'] > before['revision']
    assert 'stop_explore' in enabled['available_life_actions']
    robot.runtime.exploration_enabled = False
    stopped = robot.state_snapshot()
    assert stopped['revision'] > enabled['revision']
    assert 'stop_explore' not in stopped['available_life_actions']


@pytest.mark.asyncio
async def test_normal_interaction_may_resume_fresh_strategy_but_explicit_stop_cannot(monkeypatch):
    import importlib
    module = importlib.import_module('cloud.go2.mode.free_explore')
    nav_module = importlib.import_module('cloud.go2.navigation.navigator')
    control_module = importlib.import_module('cloud.go2.control')
    robot = robot_at()
    robot.runtime.commit('config', autonomy_mode='free_explore')
    robot.runtime.exploration_enabled = True
    monkeypatch.setattr(module, 'go2', robot)
    monkeypatch.setattr(nav_module, 'navigator', SimpleNamespace(_task=None, stop=MagicMock()))
    monkeypatch.setattr(control_module, 'control_lease', ControlLease())
    drive = module.Drive()
    drive.owner_session_id = 'same-session'
    drive._context = ExecutionContext('same-session', 'admin', True)
    assert drive.pause_for_user('same-session')
    assert robot.runtime.exploration_enabled is True
    start = MagicMock()
    monkeypatch.setattr(drive, 'start', start)
    token = robot.runtime.begin_life('play', 'same-session', 'chat-new')
    assert drive.resume_after_user() is False
    robot.runtime.finish_life(token)
    assert drive.resume_after_user() is True
    start.assert_called_once_with('same-session', drive._context)
    # A deliberate stop clears the saved permission; a later ordinary chat
    # ending cannot resurrect it.
    drive.owner_session_id = 'same-session'
    drive._paused_exploration = ('same-session', drive._context, robot.runtime.data['epoch'])
    drive.stop('same-session')
    later = robot.runtime.begin_life('play', 'same-session', 'chat-later')
    robot.runtime.finish_life(later)
    assert drive.resume_after_user() is False
    assert start.call_count == 1


@pytest.mark.asyncio
async def test_paused_strategy_does_not_take_remote_control_or_new_lease(monkeypatch):
    import importlib
    module = importlib.import_module('cloud.go2.mode.free_explore')
    nav_module = importlib.import_module('cloud.go2.navigation.navigator')
    control_module = importlib.import_module('cloud.go2.control')
    robot = robot_at()
    robot.runtime.commit('config', autonomy_mode='free_explore')
    robot.runtime.exploration_enabled = True
    monkeypatch.setattr(module, 'go2', robot)
    monkeypatch.setattr(nav_module, 'navigator', SimpleNamespace(_task=None))
    lease = ControlLease()
    monkeypatch.setattr(control_module, 'control_lease', lease)
    drive = module.Drive()
    drive._paused_exploration = ('old-owner', ExecutionContext('old-owner', 'admin', True), robot.runtime.data['epoch'])
    start = MagicMock()
    monkeypatch.setattr(drive, 'start', start)
    robot._move_control_active = True
    assert drive.resume_after_user() is False
    robot._move_control_active = False
    lease.acquire(ExecutionContext('new-user', 'visitor', False))
    assert drive.resume_after_user() is False
    start.assert_not_called()
    lease.release('new-user')
    assert drive.resume_after_user() is True
    start.assert_called_once()


def test_repeated_stationary_telemetry_does_not_publish_new_fact_each_frame():
    robot = robot_at()
    robot._motion_observation_enabled = True
    for _ in range(3):
        robot._on_state({'data': {'velocity': [0, 0, 0]}})
    before = robot.state_snapshot()
    for _ in range(10):
        robot._on_state({'data': {'velocity': [0, 0, 0]}})
    assert robot.state_snapshot()['revision'] == before['revision']


@pytest.mark.parametrize('fact', ['navigation_active', 'velocity_requested'])
def test_idle_standing_cannot_rest_until_motion_cleanup_finishes(fact):
    robot = robot_at()
    before = robot.state_snapshot()
    robot.runtime.commit('in_flight', **{fact: True})
    busy = robot.state_snapshot()
    assert busy['revision'] > before['revision']
    assert busy['body_state'] == 'standing'
    assert 'request_rest' not in busy['available_life_actions']
    with pytest.raises(ValueError, match='运动任务未结束'):
        robot.runtime.request_rest()
    robot.runtime.commit('cleanup_finished', **{fact: False})
    assert 'request_rest' in robot.state_snapshot()['available_life_actions']
