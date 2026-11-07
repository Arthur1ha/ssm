"""Search transport/permissions/media checks; never connect a physical robot."""
import time

import pytest
from fastapi import HTTPException

from cloud.go2 import activity_media
from cloud.peers.policy import permits_skill


def test_search_media_is_temporary_bounded_and_expiring(monkeypatch, tmp_path):
    monkeypatch.setattr(activity_media, '_ROOT', tmp_path)
    monkeypatch.setattr(activity_media, '_TEMP_IMAGES', activity_media.OrderedDict())
    monkeypatch.setattr(activity_media, '_MAX_IMAGES', 2)
    monkeypatch.setattr(activity_media.time, 'monotonic', lambda: 100.)
    for frame in ('a', 'b', 'c'):
        assert activity_media.save_temporary_snapshot('session', frame, b'image')
    assert activity_media.load_snapshot('session', 'a') is None
    assert activity_media.load_snapshot('session', 'c') == b'image'
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(activity_media.time, 'monotonic', lambda: 2000.)
    assert activity_media.load_snapshot('session', 'c') is None
    assert activity_media.save_temporary_snapshot('../bad', 'frame', b'image') is None


def test_search_requires_user_delegation_from_steward():
    params = {'target': '猫头鹰'}
    assert permits_skill('go2', 'go2_find_object', params, 'user_delegated', audience='steward')
    assert not permits_skill('go2', 'go2_find_object', params, 'agent_autonomous', audience='steward')
    assert not permits_skill('go2', 'go2_find_object', params, 'user_delegated', audience='peer')


@pytest.mark.asyncio
async def test_find_object_command_keeps_user_deadline_and_uses_long_task(monkeypatch):
    from cloud.api import intents
    calls = []
    async def dispatch(path, method, session, body):
        calls.append((path, method, body))
        return {'result': 'executing', 'task_id': body['task_id']}
    monkeypatch.setattr(intents, '_internal_request', dispatch)
    expires = time.time() + 80
    result = await intents._dispatch_go2('FIND_OBJECT', {'target': '猫头鹰'},
                                        {'session_id': 'visitor', 'actor': 'visitor', 'expires_at': expires})
    assert result['result'] == 'executing'
    assert calls[0][0] == '/api/go2/chat'
    assert calls[0][2]['skill_id'] == 'go2_find_object'
    assert calls[0][2]['deadline'] == expires


@pytest.mark.asyncio
async def test_visitor_cannot_change_search_bounds(monkeypatch):
    from cloud.api import intents
    with pytest.raises(HTTPException) as rejected:
        await intents._dispatch_go2('set_search_scope', {'bounds': [-1, -1, 1, 1]},
                                    {'actor': 'visitor'})
    assert rejected.value.status_code == 403


@pytest.mark.asyncio
async def test_scope_rejects_other_localization_session(monkeypatch):
    from cloud.go2 import router
    from cloud.go2.execution import ExecutionContext
    monkeypatch.setattr(router.go2, 'is_connected', True)
    monkeypatch.setattr(router.go2, '_odom_session_id', 'new')
    context = ExecutionContext('admin', 'admin', True, expires_at=int(time.time()) + 60)
    with pytest.raises(HTTPException) as rejected:
        await router.set_search_scope(router.SearchScopeRequest(session_id='old', bounds=(-1,-1,1,1)), context)
    assert rejected.value.status_code == 409


@pytest.mark.asyncio
async def test_task_stop_without_lease_still_requires_device_feedback(monkeypatch):
    from cloud.go2 import router
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.execution import ExecutionContext
    monkeypatch.setattr(router.control_lease, 'snapshot', lambda: None)
    async def stop(**_):
        return {'result': 'accepted', 'reason': 'stop_unconfirmed'}
    monkeypatch.setattr(navigator, 'stop_and_confirm', stop)
    result = await router._stop_task_motion(ExecutionContext('visitor', 'visitor', False))
    assert result['result'] == 'accepted'


@pytest.mark.asyncio
async def test_map_diagnostic_planning_does_not_block_sensor_loop(monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace
    import numpy as np
    from cloud.go2 import router
    from cloud.go2.navigation import frontier
    from cloud.go2.execution import SYSTEM_CONTEXT

    entered = threading.Event()
    release = threading.Event()
    grid = SimpleNamespace(measured_floor_z=0., has_free_space_evidence=True,
        cells=np.ones((2, 2)), obstacles=np.zeros((2, 2), dtype=bool),
        is_body_clear=lambda *args: True)
    robot = SimpleNamespace(voxel_raw={'data': {}}, occupancy_grid=grid,
        odom={'x': 0., 'y': 0., 'heading': 0.}, sensor_diagnostics={'pose': {'age_s': .01}})

    def slow_selection(*args):
        entered.set()
        if not release.wait(.8):
            raise AssertionError('event loop blocked while planning')
        return (1., 0.)

    monkeypatch.setattr(router, 'go2', robot)
    monkeypatch.setattr(frontier, 'find_frontier_target', slow_selection)
    monkeypatch.setattr(frontier, 'raycast_directions', lambda *args: {})
    monkeypatch.setattr(frontier, 'reachable_summary', lambda *args: {})
    task = asyncio.create_task(router.go2_map_debug(SYSTEM_CONTEXT))
    try:
        for _ in range(50):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        assert entered.is_set()
        assert not task.done()
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result['grid']['frontier_target'] == (1., 0.)
        assert result['diagnostics']['pose']['age_s'] == .01
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_admin_can_explicitly_configure_reachable_space_without_rectangle(monkeypatch):
    from types import SimpleNamespace
    from cloud.go2 import router
    from cloud.go2.execution import ExecutionContext
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2 import state as device_state
    from cloud.agents.go2.workflows import search
    monkeypatch.setattr(search, '_configured_scope', None)
    monkeypatch.setattr(search, '_exploration_coverage', None)
    monkeypatch.setattr(router, 'go2', SimpleNamespace(is_connected=True,
        _odom_session_id='current', fsm_state='standing'))
    monkeypatch.setattr(navigator, 'owner_session_id', None)
    monkeypatch.setattr(device_state, 'runtime_for', lambda _: SimpleNamespace(data={'life_state':'idle'}))
    context = ExecutionContext('operator', 'admin', True, expires_at=int(time.time())+60)
    req = router.SearchScopeRequest(session_id='current', mode='reachable')
    result = await router.set_search_scope(req, context)
    assert result == {'result':'ok', 'session_id':'current', 'bounds':None, 'mode':'reachable'}
    assert search.configured_search_scope()['mode'] == 'reachable'
    with pytest.raises(HTTPException) as rejected:
        await router.set_search_scope(router.SearchScopeRequest(session_id='current'), context)
    assert rejected.value.status_code == 400
