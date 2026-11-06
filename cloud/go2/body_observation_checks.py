"""Physical-state regressions using representative 2026-11-06 live joint samples.

These local checks never connect to a robot. They supplement the retained live
record, and are not a claim that another physical action was executed.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud.go2.connection import Go2Connection


# FR/FL/RR/RL, hip/thigh/calf; exact representative LOW_STATE samples.
STANDING = [-.072431, .701817, -1.404644, .035584, .704360, -1.492728,
            .003712, .783198, -1.428924, .080097, .714831, -1.407029]
SITTING = [.05, 1.28, -1.10, -.02, 1.40, -1.05,
           -.05, 2.04, -2.83, .02, 2.01, -2.79]
WAVING = [.07, -1.17, -1.47, -.22, .64, -.97,
          -.33, .98, -1.61, -.19, 1.17, -1.98]


@pytest.fixture
def observed(monkeypatch):
    import cloud.go2.connection.fsm as fsm
    import cloud.go2.connection.sensors as sensors
    clock = [1000.0]
    source = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
    monkeypatch.setattr(fsm, 'time', source)
    monkeypatch.setattr(sensors, 'time', source)
    robot = Go2Connection()
    robot.is_connected = True
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(pub_sub=SimpleNamespace(
        publish_request_new=AsyncMock(return_value={'data': {'header': {'status': {'code': 0}}}}))))

    def feed(q=STANDING, *, count=1, velocity=None, yaw=0, pitch=.023, height=.321):
        for _ in range(count):
            clock[0] += .1
            robot._on_state({'data': {'body_height': height, 'mode': 0, 'progress': 0,
                                      'velocity': velocity or [.002, .001, -.069], 'yaw_speed': yaw}})
            robot._on_low_state({'data': {'motor_state': [{'q': value, 'lost': 0} for value in q],
                                          'imu_state': {'rpy': [-.009, pitch, .1]}}})
    yield robot, feed, clock
    if robot._body_fresh_timer:
        robot._body_fresh_timer.cancel()
    if robot._action_deadline:
        robot._action_deadline.cancel()


def test_connected_robot_confirms_standing_from_joint_geometry_without_commands(observed):
    robot, feed, _ = observed
    feed(count=7)
    assert robot.fsm_state == 'standing'
    assert robot.runtime.data['body_observation_source'] == 'joint_posture'
    assert 'Hello' in robot.available_actions
    robot._conn.datachannel.pub_sub.publish_request_new.assert_not_called()


def test_real_sitting_geometry_is_not_standing_despite_overlapping_height(observed):
    robot, feed, _ = observed
    feed(SITTING, count=8, pitch=-.86, height=.31)
    assert robot.fsm_state == 'sitting'
    assert 'RiseSit' in robot.available_actions
    assert 'Hello' not in robot.available_actions


def test_invalid_joint_measurements_do_not_confirm_initial_posture(observed):
    robot, feed, _ = observed
    feed([0] * 12, count=8)
    feed([float('nan')] * 12, count=8)
    assert robot.fsm_state is None
    assert robot.available_actions == ['StopMove']


def test_missing_imu_is_not_assumed_to_be_a_level_body(observed):
    robot, _, clock = observed
    for _ in range(8):
        clock[0] += .1
        robot._on_state({'data': {'body_height': .321, 'velocity': [0, 0, -.07]}})
        robot._on_low_state({'data': {'motor_state': [{'q': q} for q in STANDING]}})
    assert robot.fsm_state is None


def test_vertical_velocity_bias_is_not_planar_movement(observed):
    robot, feed, _ = observed
    feed(count=7)
    robot._motion_observation_enabled = True
    feed(count=4, velocity=[.001, -.002, -.08])
    assert robot.fsm_state == 'standing'
    feed(velocity=[0, 0, 0], yaw=.2)
    assert robot.fsm_state == 'moving'


@pytest.mark.asyncio
async def test_motion_and_stable_target_complete_action_and_deferred_life(observed):
    robot, feed, _ = observed
    feed(count=7)
    token = robot.runtime.begin_life('play', 'visitor', 'body-test')
    task = asyncio.create_task(robot.send_command('Hello'))
    for _ in range(6):
        await asyncio.sleep(0)
    assert robot.pending_action['phase'] == 'accepted'
    feed(count=10)
    assert not task.done(), 'A stationary body plus ACK must not complete a gesture'
    assert robot.runtime.finish_life(token) is False
    assert robot.runtime.data['life_state'] == 'play'
    feed(WAVING, count=3, pitch=-.33)
    assert robot.pending_action['phase'] == 'executing'
    feed(count=12)
    result = await task
    assert result['result'] == 'ok'
    assert result['confirmation'] == 'joint_posture'
    assert robot.pending_action is None
    assert robot.runtime.data['life_state'] == 'idle'
    assert robot.runtime.data['last_action']['phase'] == 'completed'


@pytest.mark.asyncio
async def test_sit_completion_requires_joint_pattern_not_target_assignment(observed):
    robot, feed, _ = observed
    feed(count=7)
    task = asyncio.create_task(robot.send_command('Sit'))
    for _ in range(6):
        await asyncio.sleep(0)
    feed(count=10, height=.31)
    assert not task.done()
    feed(SITTING, count=12, pitch=-.86, height=.31)
    assert (await task)['result'] == 'ok'
    assert robot.fsm_state == 'sitting'


@pytest.mark.asyncio
async def test_timeout_can_restore_observed_control_without_claiming_action_success(observed):
    robot, feed, _ = observed
    feed(count=7)
    task = asyncio.create_task(robot.send_command('Hello'))
    for _ in range(6):
        await asyncio.sleep(0)
    robot._expire_body_action(robot.pending_action['request_id'])
    feed(count=12)
    assert (await task)['result'] == 'timeout'
    assert robot.pending_action is None
    assert robot.fsm_state == 'standing'
    assert robot.runtime.data['last_action']['phase'] == 'recovered'
    assert robot.runtime.data['last_action']['result'] == 'timeout'


def test_low_state_dropout_blocks_body_actions_even_with_fresh_sport(observed):
    robot, feed, clock = observed
    feed(count=7)
    clock[0] += 3
    robot._on_state({'data': {'body_height': .321, 'velocity': [0, 0, -.07]}})
    assert robot.available_actions == ['StopMove']
    assert robot.fsm_state == 'standing'


def test_fresh_low_state_cannot_use_stale_sport_to_confirm_posture(observed):
    robot, _, clock = observed
    robot._sport_state_observed_at = clock[0] - 3
    robot._robot_state = {'body_height': .321, 'velocity': [0, 0, 0]}
    for _ in range(10):
        clock[0] += .1
        robot._on_low_state({'data': {'motor_state': [{'q': q} for q in STANDING],
                                      'imu_state': {'rpy': [0, 0, 0]}}})
    assert robot.fsm_state is None
