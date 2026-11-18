"""Physical-state regressions using representative 2026-11-06 live joint samples.

These local checks never connect to a robot. They supplement the retained live
record, and are not a claim that another physical action was executed.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
    if robot._action_deadline:
        robot._action_deadline.cancel()


def test_connected_robot_confirms_standing_from_joint_geometry_without_commands(observed):
    robot, feed, _ = observed
    feed(count=7)
    assert robot.body_state == 'standing'
    assert robot.runtime.data['body_observation_source'] == 'joint_posture'
    assert 'Hello' in robot.available_actions
    robot._conn.datachannel.pub_sub.publish_request_new.assert_not_called()


def test_motion_diagnostics_preserve_real_feet_and_controller_fields(observed):
    robot, feed, _ = observed
    feed(count=7)
    robot._native_motion_mode = 'normal'
    feet = [0., .1, -.3] * 4
    robot._on_state({'data': {'body_height': .31, 'velocity': [.2, 0, 0],
        'yaw_speed': 0, 'mode': 0, 'gait_type': 0, 'error_code': 1002,
        'foot_position_body': feet, 'foot_speed_body': [0.] * 12}})
    assert robot._robot_state['native_motion_mode'] == 'normal'
    assert robot._robot_state['mode'] == robot._robot_state['gait_type'] == 0
    assert robot._robot_state['error_code'] == 1002
    assert robot._robot_state['foot_position_body'] == feet
    robot._on_state({'data': {'body_height': .31, 'velocity': [.2, 0, 0]}})
    assert robot._robot_state['foot_position_body'] is None


def test_sport_updates_do_not_expire_old_accepted_joint_feedback(observed):
    robot, feed, clock = observed
    feed(count=7)
    body_observed_at = robot.state_snapshot()['body_observed_at']
    clock[0] += 3600
    robot._on_state({'data': {'body_height': .321, 'velocity': [0, 0, 0]}})
    assert robot.body_ready_reason() is None
    assert 'Move' in robot.available_actions
    assert robot.state_snapshot()['body_observed_at'] == body_observed_at


def test_real_sitting_geometry_is_not_standing_despite_overlapping_height(observed):
    robot, feed, _ = observed
    feed(SITTING, count=8, pitch=-.86, height=.31)
    assert robot.body_state == 'sitting'
    assert 'RiseSit' in robot.available_actions
    assert 'Hello' not in robot.available_actions


def test_invalid_joint_measurements_do_not_confirm_initial_posture(observed):
    robot, feed, _ = observed
    feed([0] * 12, count=8)
    feed([float('nan')] * 12, count=8)
    assert robot.body_state is None
    assert robot.available_actions == ['StopMove']


def test_missing_imu_is_not_assumed_to_be_a_level_body(observed):
    robot, _, clock = observed
    for _ in range(8):
        clock[0] += .1
        robot._on_state({'data': {'body_height': .321, 'velocity': [0, 0, -.07]}})
        robot._on_low_state({'data': {'motor_state': [{'q': q} for q in STANDING]}})
    assert robot.body_state is None


def test_vertical_velocity_bias_is_not_planar_movement(observed):
    robot, feed, _ = observed
    feed(count=7)
    robot._motion_observation_enabled = True
    feed(count=4, velocity=[.001, -.002, -.08])
    assert robot.body_state == 'standing'
    feed(velocity=[0, 0, 0], yaw=.2)
    assert robot.body_state == 'moving'


def test_pause_is_confirmed_by_streaming_feedback_without_rpc_receipt(observed):
    robot, feed, clock = observed
    feed(count=7)
    robot._publish_velocity = Mock()  # Transport stub; no physical commands.
    robot._motion_observation_enabled = True
    feed(velocity=[.2, 0, 0])
    assert robot.body_state == 'moving'
    robot.move_velocity(0, 0, 0)
    assert robot.body_state is None and robot.body_confirmation == 'unknown'
    stopped_at = clock[0]
    feed(count=3, velocity=[0, 0, 0])
    assert clock[0] - stopped_at < .4
    assert robot.body_state == 'standing' and robot.body_confirmation == 'observed'
    robot._conn.datachannel.pub_sub.publish_request_new.assert_not_called()


def test_brief_low_speed_gait_packets_do_not_toggle_moving_to_standing(observed):
    robot, feed, clock = observed
    feed(count=7)
    robot._motion_observation_enabled = True
    robot._velocity_command_active = True
    feed(velocity=[.2, 0, 0])
    for _ in range(8):
        # Three packets can arrive in milliseconds within a physical step.
        for _ in range(5):
            clock[0] += .005
            robot._on_state({'data': {'body_height': .31,
                'velocity': [.01, .002, -.05], 'yaw_speed': 0}})
            assert robot.body_state == 'moving'
        feed(velocity=[.2, 0, 0])


def test_sustained_stationary_feedback_can_reveal_stalled_active_move(observed):
    robot, feed, _ = observed
    feed(count=7)
    robot._motion_observation_enabled = True
    robot._velocity_command_active = True
    feed(velocity=[.2, 0, 0])
    feed(count=7, velocity=[0, 0, -.05])
    assert robot.body_state == 'standing'
    assert robot.body_confirmation == 'observed'


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
@pytest.mark.parametrize('manual', [False, True])
@pytest.mark.parametrize('complete_before_ack_timeout', [False, True])
async def test_lost_rpc_ack_keeps_connection_and_completes_from_joints(observed, monkeypatch,
                                                                    complete_before_ack_timeout, manual):
    import cloud.go2.connection.webrtc as webrtc
    robot, feed, _ = observed
    feed(count=7)
    monkeypatch.setattr(webrtc, '_SPORT_RPC_TIMEOUT_S', .01)
    callbacks = {}
    pub_sub = robot._conn.datachannel.pub_sub
    pub_sub.future_resolver = SimpleNamespace(pending_callbacks=callbacks)
    sent = []

    async def missing_ack(topic, options):
        sent.append(options)
        future = asyncio.get_running_loop().create_future()
        callbacks[options['id']] = [future]
        await future

    pub_sub.publish_request_new = missing_ack
    task = asyncio.create_task(robot.send_command('Hello', dispatch_only=manual))
    for _ in range(6):
        await asyncio.sleep(0)
    request_id = robot.pending_action['request_id']
    assert robot._completion is not None
    if not complete_before_ack_timeout:
        await asyncio.sleep(.03)
        assert robot.is_connected
        assert not task.done()
    feed(WAVING, count=3, pitch=-.33)
    feed(count=12)
    result = await task
    assert result['result'] == 'ok'
    assert result['request_id'] == request_id
    assert result['confirmation'] == 'joint_posture'
    assert robot.is_connected
    assert robot.pending_action is None
    assert len(sent) == 1
    assert callbacks == {}, 'Late RPC packets must not resolve a cancelled SDK future'


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
    assert robot.body_state == 'sitting'


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
    assert robot.body_state == 'standing'
    assert robot.runtime.data['last_action']['phase'] == 'recovered'
    assert robot.runtime.data['last_action']['result'] == 'timeout'


def test_fresh_low_state_cannot_use_stale_sport_to_confirm_posture(observed):
    robot, _, clock = observed
    robot._sport_state_observed_at = clock[0] - 3
    robot._robot_state = {'body_height': .321, 'velocity': [0, 0, 0]}
    for _ in range(10):
        clock[0] += .1
        robot._on_low_state({'data': {'motor_state': [{'q': q} for q in STANDING],
                                      'imu_state': {'rpy': [0, 0, 0]}}})
    assert robot.body_state is None

@pytest.mark.parametrize('feedback', ['renewed', 'frozen', 'increasing_loss'])
def test_nonzero_motor_loss_accepts_stationary_angles_but_rejects_growing_loss(observed, feedback):
    robot, _, clock = observed
    for i in range(20):
        clock[0] += .1
        q = STANDING.copy()
        if feedback != 'frozen':
            q[0] += .0002 * (i % 3)
        motors = [{'q': value, 'lost': 0} for value in q]
        motors[0]['lost'] = 5 + (i if feedback == 'increasing_loss' else 0)
        robot._on_state({'data': {'body_height': .321, 'velocity': [0, 0, -.07], 'yaw_speed': 0}})
        robot._on_low_state({'data': {'motor_state': motors, 'imu_state': {'rpy': [0, 0, 0]}}})
    assert (robot.body_state == 'standing') is (feedback != 'increasing_loss')
    assert (robot._low_state['motor_positions'] is not None) is (feedback != 'increasing_loss')


def test_joint_feedback_evidence_does_not_cross_connection_epoch(observed):
    robot, _, clock = observed
    motors = [{'q': value, 'lost': 0} for value in STANDING]
    motors[0]['lost'] = 5
    for i in range(10):
        clock[0] += .1
        motors[0]['q'] = STANDING[0] + i * .0002
        positions = robot._motor_positions_from_feedback(motors, clock[0])
    assert positions is not None
    robot.reset_sensor_epoch()
    clock[0] += .1
    assert robot._motor_positions_from_feedback(motors, clock[0]) is None


def test_confirmed_motor_feedback_can_be_stationary_without_becoming_offline(observed):
    robot, _, clock = observed
    motors = [{'q': value, 'lost': 0} for value in STANDING]
    motors[0]['lost'] = 5
    for i in range(10):
        clock[0] += .1
        motors[0]['q'] = STANDING[0] + i * .0002
        assert robot._motor_positions_from_feedback(motors, clock[0]) is not None or i < 6
    for _ in range(25):
        clock[0] += .1
        assert robot._motor_positions_from_feedback(motors, clock[0]) is not None
    # A new loss or a receive gap invalidates the old liveness proof.
    motors[0]['lost'] += 1
    assert robot._motor_positions_from_feedback(motors, clock[0]) is None
    clock[0] += 2
    assert robot._motor_positions_from_feedback(motors, clock[0]) is None


@pytest.mark.parametrize('timeout,error', [(True, 0), (False, 0x100), (False, 0x20)])
def test_active_motor_fault_blocks_despite_valid_angles_and_fresh_sport(observed, timeout, error):
    robot, feed, clock = observed
    feed(count=7)
    motors = [{'q': value, 'lost': 5, 'reserve': [0, 800]} for value in STANDING]
    motors[0]['reserve'][0] = error
    for _ in range(10):
        clock[0] += .1
        robot._on_state({'data': {'body_height': .321, 'velocity': [0, 0, 0]}})
        robot._on_low_state({'data': {'motor_state': motors, 'bit_flag': 0x80 if timeout else 0,
                                    'imu_state': {'rpy': [0, 0, 0]}}})
    assert robot._low_state['motor_positions'] is None
    assert robot.body_ready_reason() == 'body_telemetry_unavailable'
    assert robot.available_actions == ['StopMove']


@pytest.mark.asyncio
async def test_physical_completion_does_not_wait_for_rpc_timeout(observed, monkeypatch):
    import cloud.go2.connection.webrtc as webrtc
    robot, feed, _ = observed
    feed(count=7)
    monkeypatch.setattr(webrtc, '_SPORT_RPC_TIMEOUT_S', 60)
    cancelled = asyncio.Event()

    async def no_ack(*args):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    robot._conn.datachannel.pub_sub.publish_request_new = no_ack
    task = asyncio.create_task(robot.send_command('Hello', dispatch_only=True))
    for _ in range(10):
        await asyncio.sleep(0)
    feed(count=12)
    assert not task.done(), 'Unchanged standing is not a completed greeting'
    feed(WAVING, count=3, pitch=-.33)
    feed(count=12)
    assert (await asyncio.wait_for(task, .2))['result'] == 'ok'
    assert cancelled.is_set()


def test_low_state_before_sport_is_paired_once_without_losing_motor_freshness(observed):
    robot, feed, clock = observed
    feed(count=7)
    clock[0] += .6
    robot._on_low_state({'data': {'motor_state': [{'q': q} for q in STANDING],
                                  'imu_state': {'rpy': [0, 0, 0]}}})
    assert robot._last_motor_observed_at == clock[0]
    before = robot._posture_candidate_samples
    robot._on_state({'data': {'body_height': .321, 'velocity': [0, 0, 0]}})
    assert robot._posture_candidate_samples == before + 1
    robot._on_state({'data': {'body_height': .321, 'velocity': [0, 0, 0]}})
    assert robot._posture_candidate_samples == before + 1


def test_known_stable_motor_loss_survives_short_network_gap(observed):
    robot, _, clock = observed
    motors = [{'q': q, 'lost': 5} for q in STANDING]
    for _ in range(8):
        clock[0] += .1
        robot._motor_positions_from_feedback(motors, clock[0])
    clock[0] += 1.2
    assert robot._motor_positions_from_feedback(motors, clock[0]) is not None
    motors[0]['lost'] += 1
    assert robot._motor_positions_from_feedback(motors, clock[0]) is None
