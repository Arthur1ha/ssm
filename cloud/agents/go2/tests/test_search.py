import asyncio
import time

import pytest

from cloud.agents.go2.workflows.search import build_search_graph, search_bounds, SEARCH_MAX_STEPS


@pytest.fixture(autouse=True)
def _load_executor_before_connection_overrides():
    # run_search lazily imports the executor, whose module-level go2 binding
    # must capture the real adapter before individual tests replace connection.go2.
    # Restoring connection.go2 alone cannot restore a cached imported binding.
    from importlib import import_module
    import_module('cloud.go2.tools.executor')


class Replay:
    def __init__(self, observations, *, choices=None, reason=None, approach=None):
        self.observations = iter(observations)
        self.choices = iter(choices or [])
        self.calls = []
        self.reason = reason
        self.arrival = approach or {"result": "ok", "reason": "arrival_verified"}

    async def guard(self, state):
        return self.reason

    async def observe(self, state):
        self.calls.append("observe")
        return next(self.observations)

    async def record(self, observation):
        return {"unchecked_known_cells": 10, "occluded_or_unknown": "not_checked"}

    async def progress(self, *args, **kwargs):
        pass

    async def select(self, state):
        self.calls.append("select")
        return next(self.choices, {"reason": "coverage_exhausted"})

    async def navigate(self, state):
        self.calls.append("navigate")
        return {"result": "ok", "distance_m": 1.0, "execution_id": "one"}

    async def approach(self, state):
        self.calls.append("approach")
        return self.arrival


def initial(**changes):
    return {"task_id": "task", "mode": "find_object", "target": "owl",
            "map_session": "map", "phase": "observe", "steps": 0,
            "deadline": time.time() + 60, "distance_m": 0.0, **changes}


async def run(adapter, **changes):
    return await build_search_graph(adapter).ainvoke(initial(**changes))


@pytest.mark.asyncio
async def test_visible_target_is_confirmed_before_any_motion():
    adapter = Replay([{"found": True, "frame_id": 1}, {"found": True, "frame_id": 2}])
    state = await run(adapter)
    assert state["result"]["result"] == "ok"
    assert adapter.calls == ["observe", "observe", "approach"]


@pytest.mark.asyncio
async def test_same_frame_never_counts_as_confirmation():
    adapter = Replay([{"found": True, "frame_id": 1}] * 2)
    state = await run(adapter)
    assert state["result"]["reason"] == "confirmation_frame_reused"
    assert "approach" not in adapter.calls


@pytest.mark.asyncio
async def test_out_of_view_target_requires_new_observation_after_move():
    adapter = Replay([{"found": False, "frame_id": 1}, {"found": True, "frame_id": 2},
                      {"found": True, "frame_id": 3}], choices=[{"target": {"path_cost_m": 1.0}}])
    state = await run(adapter)
    assert adapter.calls == ["observe", "select", "navigate", "observe", "observe", "approach"]
    assert state["result"]["steps"] == 1


@pytest.mark.asyncio
async def test_absence_does_not_claim_object_nonexistent():
    state = await run(Replay([{"found": False, "frame_id": 1}]))
    assert state["result"]["result"] == "blocked"
    assert state["result"]["found"] is False
    assert state["result"]["coverage"]["unchecked_known_cells"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["execution_context_expired", "map_session_changed", "control_lease_changed", "stop_unconfirmed"])
async def test_guard_never_observes_or_moves_after_invalid_authority(reason):
    adapter = Replay([], reason=reason)
    state = await run(adapter)
    assert state["result"]["reason"] == reason
    assert not adapter.calls


@pytest.mark.asyncio
async def test_budget_is_explicit_not_parent_runner_turn_limit():
    adapter = Replay([])
    state = await run(adapter, steps=SEARCH_MAX_STEPS)
    assert state["result"]["reason"] == "step_budget_exhausted"
    assert not adapter.calls


@pytest.mark.asyncio
async def test_found_photo_survives_failed_approach():
    adapter = Replay([{"found": True, "frame_id": 1}, {"found": True, "frame_id": 2, "media_ref": "/photo"}],
                     approach={"result": "blocked", "reason": "calibration_unavailable"})
    state = await run(adapter)
    assert state["result"]["found"] is True
    assert state["result"]["result"] == "blocked"
    assert state["result"]["evidence"]["media_ref"] == "/photo"


def test_no_implicit_room_or_infinite_bounds(monkeypatch):
    for bounds in ("", "0,0,inf,1", "1,0,0,1", "0,1,2"):
        monkeypatch.setenv("GO2_SEARCH_BOUNDS", bounds)
        with pytest.raises(ValueError, match="search_scope_required"):
            search_bounds()
    monkeypatch.setenv("GO2_SEARCH_BOUNDS", "-1,-2,3,4")
    assert search_bounds() == (-1, -2, 3, 4)


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_result,expected', [('ok','timeout'), ('accepted','accepted')])
async def test_server_owned_search_deadline_and_retry_do_not_replay(monkeypatch, tmp_path, stop_result, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from cloud.agents.go2.workflows import search
    from cloud.go2 import connection, control, state as device_state
    from cloud.go2.execution import ExecutionContext
    from cloud.go2.navigation.navigator import navigator
    monkeypatch.setenv('SSM_CHECKPOINT_DIR', str(tmp_path))
    monkeypatch.setattr(search, '_configured_scope', ((-1.,-1.,1.,1.), 'map'))
    monkeypatch.setattr(connection, 'go2', SimpleNamespace(_odom_session_id='map'))
    lease = control.ControlLease()
    monkeypatch.setattr(control, 'control_lease', lease)
    runtime = SimpleNamespace(data={'life_state':'idle'}, exploration_enabled=True,
                              begin_life=lambda *args: 'token', finish_life=lambda *args: None)
    monkeypatch.setattr(device_state, 'runtime_for', lambda _: runtime)
    stop = AsyncMock(return_value={'result':stop_result})
    monkeypatch.setattr(navigator, 'stop_and_confirm', stop)
    calls = []
    class Waiting(Replay):
        def __init__(self, *args):
            super().__init__([])
            self.travelled_m = 0.
        async def observe(self, state):
            calls.append('observe')
            await asyncio.Event().wait()
    monkeypatch.setattr(search, 'DeviceSearch', Waiting)
    context = ExecutionContext('visitor','visitor',False,expires_at=int(time.time())+60)
    result = await search.run_search('owl', context=context, task_id='one', deadline=time.time()+.05)
    assert result['result'] == expected
    assert result['reason'] == ('time_budget_exhausted' if stop_result=='ok' else 'stop_unconfirmed')
    count = len(calls)
    retry = await search.run_search('owl', context=context, task_id='one', deadline=time.time()+10)
    assert retry['result'] == expected and len(calls) == count
    assert stop.await_count == 1
    if stop_result != 'ok':
        assert runtime.exploration_enabled is False


@pytest.mark.asyncio
async def test_device_selection_with_numpy_map_coordinates_survives_checkpoint(monkeypatch, tmp_path):
    """Production selector + adapter + serializer, with real voxel metadata types."""
    import numpy as np
    from cloud.agents.go2.workflows.search import DeviceSearch
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.go2.execution import ExecutionContext
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.navigation.occupancy_grid import OccupancyGrid, FREE

    # Shape/resolution/origin and pose are representative of the field recording;
    # use synthetic free cells so the regression has no private artifact dependency.
    grid = OccupancyGrid({'data': {'frame_id': 'odom', 'resolution': .05,
        'origin': [-3.225, -3.175, -.575], 'width': [128, 128, 38],
        'data': {'points': []}}})
    grid.origin = np.asarray(grid.origin)
    grid.cells[:] = FREE
    grid.known_floor[:] = True
    grid.grid[:] = False
    grid.map_session = 'map'
    pose = {'x': .014612, 'y': .018113, 'heading': .164683, 'session_id': 'map'}
    monkeypatch.setattr(navigator, 'snapshot', lambda: ((pose, grid), None))
    context = ExecutionContext('test', 'admin', False, expires_at=int(time.time()) + 60)
    adapter = DeviceSearch(context, 'task', (-.485364, -.681975, 1.514636, .718025), 'map')
    observation = {'pose': pose, 'image_time': time.monotonic(), 'quality': 1., 'found': False}
    assert (await adapter.record(observation))['observations'] == 1
    selection = await adapter.select({})
    candidate = selection['target']
    assert candidate is not None
    saver = FileCheckpointSaver(tmp_path)
    kind, encoded = saver.serde.dumps_typed({'candidate': candidate})
    restored = saver.serde.loads_typed((kind, encoded))
    assert restored['candidate'] == candidate
    assert all(type(candidate[k]) is float for k in ('x', 'y', 'heading', 'score', 'path_cost_m'))


@pytest.mark.asyncio
async def test_failed_node_preserves_completed_coverage_and_logs_safe_diagnostics(monkeypatch, tmp_path, caplog):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from cloud.agents.go2.workflows import search
    from cloud.go2 import connection, control, state as device_state
    from cloud.go2.execution import ExecutionContext
    from cloud.go2.navigation.navigator import navigator
    monkeypatch.setenv('SSM_CHECKPOINT_DIR', str(tmp_path))
    monkeypatch.setattr(search, '_configured_scope', ((-1., -1., 1., 1.), 'map'))
    monkeypatch.setattr(connection, 'go2', SimpleNamespace(_odom_session_id='map'))
    monkeypatch.setattr(control, 'control_lease', control.ControlLease())
    runtime = SimpleNamespace(data={'life_state': 'idle'}, exploration_enabled=True,
        begin_life=lambda *args: 'token', finish_life=lambda *args: None)
    monkeypatch.setattr(device_state, 'runtime_for', lambda _: runtime)
    stop = AsyncMock(return_value={'result': 'ok'})
    monkeypatch.setattr(navigator, 'stop_and_confirm', stop)

    class BrokenSelection(Replay):
        def __init__(self, *args):
            super().__init__([{'found': False, 'frame_id': 1}])
            self.travelled_m = 0.
        async def select(self, state):
            raise TypeError('private credential must not be logged')

    monkeypatch.setattr(search, 'DeviceSearch', BrokenSelection)
    context = ExecutionContext('test', 'admin', False, expires_at=int(time.time()) + 60)
    result = await search.run_search('owl', context=context, task_id='failed-selection')
    assert result['reason'] == 'search_step_failed'
    assert result['coverage']['unchecked_known_cells'] == 10
    assert result['error_type'] == 'TypeError'
    assert 'TypeError' in caplog.text and 'failed-selection' in caplog.text
    assert 'private credential' not in caplog.text
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_navigation_blocked_receipt_survives_search_checkpoint(monkeypatch, tmp_path):
    """Do not hide real Navigator receipt types behind a canned adapter result."""
    from types import SimpleNamespace
    import numpy as np
    from cloud.agents.common.checkpoint import FileCheckpointSaver
    from cloud.go2.navigation import navigator as nav_module
    from cloud.go2.navigation.occupancy_grid import OccupancyGrid, FREE

    grid = OccupancyGrid({'data': {'frame_id': 'odom', 'resolution': .05,
        'origin': [-1., -1., 0.], 'width': [40, 40, 38], 'data': {'points': []}}})
    grid.origin = np.asarray(grid.origin)
    grid.cells[:] = FREE
    grid.grid[:] = False
    grid.known_floor[:] = True
    # The path is clear, but rotating the rectangular body crosses unmeasured
    # ground. This must remain a genuine blocked receipt, not become an error.
    ix, iy = grid.odom_to_grid(.30, .30)
    grid.known_floor[iy, ix] = False
    grid.has_free_space_evidence = True
    commands = []
    now = time.monotonic()
    robot = SimpleNamespace(is_connected=True,
        odom={'x': 0., 'y': 0., 'heading': float(np.pi / 2)},
        odom_received_at=now, voxel_received_at=now, odom_frame_id='odom', voxel_frame_id='odom',
        _odom_session_id='map', occupancy_grid=grid,
        move_velocity=lambda *args, **kwargs: commands.append(args))
    monkeypatch.setattr(nav_module, 'go2', robot)
    nav = nav_module.Navigator()

    class WithRealNavigation(Replay):
        async def navigate(self, state):
            receipt = await nav.go_to_coordinate(.55, -.15)
            return {**receipt, 'result': receipt['status']}

    adapter = WithRealNavigation([{'found': False, 'frame_id': 1}],
        choices=[{'target': {'x': .55, 'y': -.15, 'path_cost_m': .61}}])
    saver = FileCheckpointSaver(tmp_path)
    config = {'configurable': {'thread_id': 'real-navigation-receipt'}}
    graph = build_search_graph(adapter, checkpointer=saver)
    state = await graph.ainvoke(initial(), config, durability='sync')
    assert state['result']['reason'] == 'turn_clearance_unavailable'
    receipt = state['result']['navigation']
    assert receipt['status'] == 'blocked'
    assert receipt['actual_goal'] is not None
    assert type(receipt['actual_goal']['x']) is float
    assert type(receipt['actual_goal']['y']) is float
    assert commands and all(command == (0, 0, 0) for command in commands)
    restored = await graph.aget_state(config)
    assert restored.values['result']['navigation'] == receipt


def test_reachable_scope_is_explicit_and_bounded_default_is_preserved(monkeypatch):
    from cloud.agents.go2.workflows import search
    monkeypatch.setattr(search, '_configured_scope', None)
    monkeypatch.setattr(search, '_exploration_coverage', None)
    monkeypatch.delenv('GO2_SEARCH_BOUNDS', raising=False)
    with pytest.raises(ValueError, match='search_scope_required'):
        search.search_bounds()
    with pytest.raises(ValueError, match='invalid_search_scope'):
        search.configure_search_scope(None, 'map')
    search.configure_search_scope((-1, -2, 3, 4), 'map')
    assert search.search_bounds() == (-1., -2., 3., 4.)
    assert search.configured_search_scope()['mode'] == 'bounded'
    search.configure_search_scope(None, 'map', mode='reachable')
    assert search.search_bounds() is None
    assert search.configured_search_scope() == {'mode': 'reachable', 'bounds': None, 'session_id': 'map'}
    with pytest.raises(ValueError, match='reachable_scope_has_no_rectangle'):
        search.configure_search_scope((-1, -2, 3, 4), 'map', mode='reachable')


@pytest.mark.asyncio
async def test_reachable_scope_follows_map_growth_but_not_session_change(monkeypatch):
    from types import SimpleNamespace
    from cloud.agents.go2.workflows.search import DeviceSearch
    from cloud.go2.execution import ExecutionContext
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.navigation.occupancy_grid import OccupancyGrid, FREE

    def free_grid(origin, width, session='map'):
        grid = OccupancyGrid({'data': {'frame_id': 'odom', 'resolution': .1,
            'origin': origin, 'width': width, 'data': {'points': []}}})
        grid.cells[:] = FREE
        grid.known_floor[:] = True
        grid.grid[:] = False
        grid.map_session = session
        return grid

    context = ExecutionContext('test', 'admin', False, expires_at=int(time.time())+60)
    adapter = DeviceSearch(context, 'reachable-task', None, 'map')
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 'map'}
    small = free_grid([-1., -1., -.5], [20, 20, 10])
    monkeypatch.setattr(navigator, 'snapshot', lambda: ((pose, small), None))
    first = await adapter.record({'pose': pose, 'image_time': 1., 'quality': 1., 'found': False})
    assert first['observations'] == 1
    assert adapter.bounds == (-1., -1., 1., 1.)

    expanded = free_grid([-2., -2., -.5], [70, 40, 10])
    pose = {**pose, 'x': 2.}
    monkeypatch.setattr(navigator, 'snapshot', lambda: ((pose, expanded), None))
    second = await adapter.record({'pose': pose, 'image_time': 2., 'quality': 1., 'found': False})
    assert adapter.bounds == (-2., -2., 5., 2.)
    assert second['observations'] == 2
    assert second['observed_cells'] > first['observed_cells']
    assert any(x >= 20 for x, y in adapter.coverage._seen)

    expanded.map_session = 'other'
    assert adapter.refresh_scope((pose, expanded)) == 'map_session_changed'
    monkeypatch.setattr(navigator, 'snapshot', lambda: (None, 'map_unavailable'))
    assert (await adapter.select({}))['reason'] == 'map_unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('reachable', [False, True])
async def test_scope_monitor_does_not_add_a_freshness_gate_after_initialization(monkeypatch, reachable):
    from types import SimpleNamespace
    from cloud.agents.go2.workflows.search import DeviceSearch
    from cloud.go2 import connection, control, state as device_state
    from cloud.go2.execution import ExecutionContext
    from cloud.go2.navigation.navigator import navigator
    context = ExecutionContext('scope-monitor', 'admin', False, expires_at=int(time.time()) + 60)
    lease = control.ControlLease()
    lease.acquire(context)
    monkeypatch.setattr(control, 'control_lease', lease)
    monkeypatch.setattr(device_state, 'runtime_for', lambda _: None)
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 'map'}
    robot = SimpleNamespace(is_connected=True, _odom_session_id='map', odom=pose,
                            odom_received_at=time.monotonic())
    monkeypatch.setattr(connection, 'go2', robot)
    grid = SimpleNamespace(map_session='map', origin=(-1., -1.), width=(20, 20), resolution=.1)
    monkeypatch.setattr(navigator, 'snapshot', lambda: ((pose, grid), None))
    adapter = DeviceSearch(context, 'scope-monitor', None if reachable else (-1., -1., 1., 1.), 'map')
    assert await adapter.guard({}) is None
    assert adapter.bounds == (-1., -1., 1., 1.)

    # Both modes leave gaps to the existing Navigator/capture gates. A new
    # selection still fails without a fresh snapshot; no motion gate is bypassed.
    robot.odom_received_at = time.monotonic() - 2
    monkeypatch.setattr(navigator, 'snapshot', lambda: (None, 'localization_unavailable'))
    assert await adapter.guard({}) is None
    assert (await adapter.select({}))['reason'] == 'localization_unavailable'
    robot._odom_session_id = 'new-map'
    assert await adapter.guard({}) == 'map_session_changed'


@pytest.mark.asyncio
async def test_monitor_failure_preserves_completed_navigation_receipt(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from cloud.agents.go2.workflows import search
    from cloud.go2 import connection, control, state as device_state
    from cloud.go2.execution import ExecutionContext
    from cloud.go2.navigation.navigator import navigator
    monkeypatch.setenv('SSM_CHECKPOINT_DIR', str(tmp_path))
    monkeypatch.setattr(search, '_configured_scope', ((-1., -1., 1., 1.), 'map'))
    monkeypatch.setattr(connection, 'go2', SimpleNamespace(_odom_session_id='map'))
    monkeypatch.setattr(control, 'control_lease', control.ControlLease())
    runtime = SimpleNamespace(data={'life_state': 'idle'}, exploration_enabled=True,
        begin_life=lambda *args: 'token', finish_life=lambda *args: None)
    monkeypatch.setattr(device_state, 'runtime_for', lambda _: runtime)
    monkeypatch.setattr(navigator, 'stop_and_confirm', AsyncMock(return_value={'result': 'ok'}))

    class InterruptedAfterMove(Replay):
        def __init__(self, *args):
            super().__init__([{'found': False, 'frame_id': 1}],
                             choices=[{'target': {'path_cost_m': 1.}}])
            self.travelled_m = 0.
            self.observed = False
            self.monitor_failed = False
        async def observe(self, state):
            if self.observed:
                self.monitor_failed = True
                await asyncio.Event().wait()
            self.observed = True
            return await super().observe(state)
        async def navigate(self, state):
            self.travelled_m = 1.756
            return {'result': 'ok', 'status': 'succeeded', 'distance_m': 1.756,
                    'task_id': 'real-navigation', 'execution_id': 'search:0:navigate'}
        async def guard(self, state):
            return 'control_lease_changed' if self.monitor_failed else None

    monkeypatch.setattr(search, 'DeviceSearch', InterruptedAfterMove)
    context = ExecutionContext('moving-test', 'admin', False, expires_at=int(time.time())+60)
    result = await search.run_search('owl', context=context, task_id='interrupted-after-navigation')
    assert result['reason'] == 'control_lease_changed'
    assert result['steps'] == 1
    assert result['distance_m'] == 1.756
    assert result['navigation']['status'] == 'succeeded'
    assert result['navigation']['execution_id'] == 'search:0:navigate'
    assert result['coverage']['unchecked_known_cells'] == 10


def transit_device(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from cloud.agents.go2.workflows.search import DeviceSearch
    from cloud.go2 import connection
    from cloud.go2.execution import ExecutionContext
    from cloud.go2.perception import visual_target
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 'map'}
    robot = SimpleNamespace(odom=pose, latest_frame_snapshot=lambda: (b'jpeg', {
        'frame_id': 1, 'received_at': time.monotonic(), 'odom_session_id': 'map',
        'width': 640, 'height': 360}))
    monkeypatch.setattr(connection, 'go2', robot)
    monkeypatch.setattr(visual_target, 'image_quality', lambda _: 1.)
    context = ExecutionContext('test', 'admin', False, expires_at=int(time.time()) + 60)
    adapter = DeviceSearch(context, 'transit', (-2., -2., 2., 2.), 'map')
    adapter.guard = AsyncMock(return_value=None)
    async def invoke_effect(state, name, params, invoke):
        return await invoke()
    adapter.effect = invoke_effect
    return adapter, robot


@pytest.mark.asyncio
async def test_transit_candidate_stops_navigation_and_requires_fresh_graph_confirmation(monkeypatch):
    from unittest.mock import AsyncMock
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.perception import visual_target
    adapter, robot = transit_device(monkeypatch)
    cancelled = asyncio.Event()
    async def moving(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    stop = AsyncMock(return_value={'result': 'ok'})
    monkeypatch.setattr(navigator, 'go_to_coordinate', moving)
    monkeypatch.setattr(navigator, 'stop_and_confirm', stop)
    monkeypatch.setattr(visual_target, 'detect_visible_target', AsyncMock(
        return_value={'found': True, 'bbox_1000': [100, 100, 200, 200]}))
    candidate = {'x': 1., 'y': 0., 'heading': 0., 'path_cost_m': 1.}
    receipt = await asyncio.wait_for(adapter.navigate(initial(candidate=candidate)), 1)
    assert receipt['result'] == 'ok' and receipt['reason'] == 'transit_candidate_stopped'
    assert 'found' not in receipt and receipt['transit_candidate']['frame_id'] == 1
    assert cancelled.is_set()
    stop.assert_awaited_once_with(owner_session_id='test')
    # The movement image is not one of the two required stopped observations.
    replay = Replay([{'found': False, 'frame_id': 0}, {'found': True, 'frame_id': 2},
                     {'found': True, 'frame_id': 3}], choices=[{'target': candidate}])
    replay.navigate = AsyncMock(return_value=receipt)
    result = await run(replay)
    assert replay.calls.count('observe') == 3
    assert result['result']['result'] == 'ok' and 'approach' in replay.calls


@pytest.mark.asyncio
async def test_transit_miss_does_not_interrupt_navigation_or_mark_coverage(monkeypatch):
    from unittest.mock import AsyncMock
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.perception import visual_target
    adapter, robot = transit_device(monkeypatch)
    observed = asyncio.Event()
    async def detect(*args):
        observed.set()
        return {'found': False, 'bbox_1000': None}
    async def moving(*args, **kwargs):
        await observed.wait()
        return {'status': 'succeeded', 'last_trusted_pose': robot.odom}
    stop = AsyncMock(return_value={'result': 'ok'})
    monkeypatch.setattr(navigator, 'go_to_coordinate', moving)
    monkeypatch.setattr(navigator, 'stop_and_confirm', stop)
    monkeypatch.setattr(visual_target, 'detect_visible_target', detect)
    receipt = await asyncio.wait_for(adapter.navigate(initial(
        candidate={'x': 1., 'y': 0., 'heading': 0.})), 1)
    assert receipt['result'] == 'ok'
    assert not adapter.coverage.records
    stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('distance, expected', [(.0, 'blocked'), (.17, 'ok')])
async def test_search_reposition_requires_actual_motion_and_fresh_next_view(monkeypatch, distance, expected):
    from unittest.mock import AsyncMock
    from cloud.go2.navigation.navigator import navigator
    adapter, robot = transit_device(monkeypatch)
    robot.odom = {**robot.odom, 'session_id': 'map'}
    start = robot.odom.copy()
    moved = {**start, 'x': start['x']+distance}
    navigate = AsyncMock(return_value={'status': 'succeeded', 'last_trusted_pose': moved})
    monkeypatch.setattr(navigator, 'go_to_coordinate', navigate)
    before = time.monotonic()
    receipt = await adapter.navigate(initial(mode='explore', candidate={
        'x': start['x']+.30, 'y': start['y'], 'heading': start['heading'],
        'source': 'observation_reposition', 'path_cost_m': .30}))
    assert receipt['result'] == expected
    assert navigate.call_args.kwargs['arrival_tolerance_m'] == .08
    assert navigate.call_args.kwargs['minimum_command_speed'] == .25
    assert navigate.call_args.kwargs['speed_limit'] == .30
    assert adapter.after_time >= before
    assert not adapter.coverage.records
    if expected == 'blocked':
        assert receipt['reason'] == 'observation_move_unconfirmed'
        assert receipt['navigation_receipt']['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_transit_cancel_joins_navigation_and_model_observer(monkeypatch):
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.perception import visual_target
    adapter, _ = transit_device(monkeypatch)
    started, detector_closed, navigation_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def detect(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            detector_closed.set()
    async def moving(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            navigation_closed.set()
    monkeypatch.setattr(navigator, 'go_to_coordinate', moving)
    monkeypatch.setattr(visual_target, 'detect_visible_target', detect)
    task = asyncio.create_task(adapter.navigate(initial(candidate={'x': 1., 'y': 0., 'heading': 0.})))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert detector_closed.is_set() and navigation_closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('stale', [True, False])
async def test_transit_ignores_stale_or_other_session_frames(monkeypatch, stale):
    from unittest.mock import AsyncMock
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.perception import visual_target
    adapter, robot = transit_device(monkeypatch)
    robot.latest_frame_snapshot = lambda: (b'jpeg', {'frame_id': 1,
        'received_at': time.monotonic() - (2 if stale else 0),
        'odom_session_id': 'map' if stale else 'another-session'})
    async def moving(*args, **kwargs):
        await asyncio.sleep(.03)
        return {'status': 'succeeded', 'last_trusted_pose': robot.odom}
    detect = AsyncMock()
    monkeypatch.setattr(navigator, 'go_to_coordinate', moving)
    monkeypatch.setattr(visual_target, 'detect_visible_target', detect)
    receipt = await asyncio.wait_for(adapter.navigate(initial(
        candidate={'x': 1., 'y': 0., 'heading': 0.})), 1)
    assert receipt['result'] == 'ok'
    detect.assert_not_awaited()


@pytest.mark.asyncio
async def test_transit_candidate_after_authority_loss_only_stops(monkeypatch):
    from unittest.mock import AsyncMock
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.perception import visual_target
    adapter, _ = transit_device(monkeypatch)
    adapter.guard = AsyncMock(side_effect=[None, 'execution_context_expired'])
    async def moving(*args, **kwargs):
        await asyncio.Event().wait()
    stop = AsyncMock(return_value={'result': 'ok'})
    monkeypatch.setattr(navigator, 'go_to_coordinate', moving)
    monkeypatch.setattr(navigator, 'stop_and_confirm', stop)
    monkeypatch.setattr(visual_target, 'detect_visible_target', AsyncMock(
        return_value={'found': True, 'bbox_1000': [100, 100, 200, 200]}))
    receipt = await asyncio.wait_for(adapter.navigate(initial(
        candidate={'x': 1., 'y': 0., 'heading': 0.})), 1)
    assert receipt['result'] == 'blocked'
    assert receipt['reason'] == 'execution_context_expired'
    assert receipt['transit_candidate'] is None
    stop.assert_awaited_once()
