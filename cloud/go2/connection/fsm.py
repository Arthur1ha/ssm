import logging
import math
import time

from cloud.go2.actions import ACTIONS, SPORT_COMMANDS, TRANSIENT_STATES, fsm_transitions
from cloud.go2.state import Go2State, STABLE_BODY_STATES

logger = logging.getLogger(__name__)


class CommandBlocked(ValueError):
    """A valid command is not allowed in the current device state."""

_FSM_NEXT = fsm_transitions()
_FSM_AVAILABLE = {"offline": [], "connecting": [],
                  **{state: list(edges) for state, edges in _FSM_NEXT.items()}}
_TRANSIENT_STATES = set(TRANSIENT_STATES)


class Go2FSM:
    """Body transition rules; only supported observations confirm completion."""

    def __init__(self) -> None:
        super().__init__()
        self.runtime = Go2State(self._state_changed)
        self._last_progress = 0.0
        self._stationary_samples = 0
        self._motion_observation_enabled = False
        self._last_motor_positions = None
        self._last_motor_observed_at = None
        self._joint_observation_available = False
        self._posture_candidate = None
        self._posture_candidate_since = None
        self._posture_candidate_samples = 0
        self._posture_candidate_positions = None
        self._action_observation = None

    def _reset_body_observations(self):
        self._last_motor_positions = None
        self._last_motor_observed_at = None
        self._joint_observation_available = False
        self._posture_candidate = None
        self._posture_candidate_since = None
        self._posture_candidate_samples = 0
        self._posture_candidate_positions = None
        self._action_observation = None

    def _state_changed(self):
        receipt_callback = getattr(self, "_command_result_callback", None)
        if receipt_callback is not None:
            receipt_callback()
        publisher = getattr(self, "_peer_state_callback", None)
        if publisher is not None:
            publisher()
        self._push_state_queues()
        resume = getattr(self, '_autonomy_resume_callback', None)
        if resume is not None:
            resume()

    def _push_state_queues(self):
        import asyncio
        payload = self.state_snapshot()
        def enqueue(q):
            if q.full():
                q.get_nowait()
            q.put_nowait(payload)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for q in tuple(getattr(self, '_state_queues', ())):
            loop = getattr(self, '_state_queue_loops', {}).get(q)
            if loop is not None and loop is not current_loop:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(enqueue, q)
            else:
                enqueue(q)

    @property
    def fsm_state(self):
        return self.runtime.snapshot()["fsm_state"]

    @fsm_state.setter
    def fsm_state(self, new):
        # Compatibility writes are events into the fact store, never a second state.
        if new in {"offline", "connecting"}:
            self.runtime.connection(new)
        else:
            self.runtime.body(new)

    @property
    def is_connected(self):
        return self.runtime.data['connected']

    @is_connected.setter
    def is_connected(self, value):
        self.runtime.connection('online' if value else 'offline')

    @property
    def _velocity_command_active(self):
        return self.runtime.data['velocity_requested']

    @_velocity_command_active.setter
    def _velocity_command_active(self, value):
        self.runtime.commit('velocity_requested', velocity_requested=bool(value))

    @property
    def _move_control_active(self):
        return self.runtime.data['control_mode'] == 'remote'

    @_move_control_active.setter
    def _move_control_active(self, value):
        self.runtime.commit('control_mode', control_mode='remote' if value else 'normal')

    @property
    def body_confirmation(self):
        return self.runtime.data['body_confirmation']

    @body_confirmation.setter
    def body_confirmation(self, value):
        self.runtime.commit('body_confirmation', body_confirmation=value)

    @property
    def pending_action(self):
        from copy import deepcopy
        return deepcopy(self.runtime.data['pending_action'])

    @pending_action.setter
    def pending_action(self, value):
        self.runtime.commit('body_execution', pending_action=value)

    def _refresh_body_freshness(self):
        observed = getattr(self, '_sport_state_observed_at', None)
        now = time.monotonic()
        motor_stale = self._joint_observation_available and (self._last_motor_observed_at is None
                                                            or now - self._last_motor_observed_at > 2)
        if observed is not None and (now - observed > 2 or motor_stale):
            self.runtime.commit('telemetry_expired', body_telemetry_fresh=False)

    def body_ready_reason(self, *, refresh=True):
        if refresh:
            self._refresh_body_freshness()
        if not self.runtime.data['body_telemetry_fresh']:
            return 'body_telemetry_stale'
        if self.body_confirmation not in {'observed', 'previous_observation'}:
            return '等待有效设备反馈'
        return None

    def state_snapshot(self):
        self._refresh_body_freshness()
        with self.runtime._lock:
            result = self.runtime.snapshot()
            available = self._available_actions()
            result['available_actions'] = available
            manual_reason = self.runtime.active_reason()
            manual_commands = [cmd for cmd in SPORT_COMMANDS if cmd != 'StopMove']
            result['available_manual_actions'] = [] if manual_reason else manual_commands
            result['blocked_manual_actions'] = ({cmd: manual_reason for cmd in manual_commands}
                                                if manual_reason else {})
            reason = self.runtime.active_reason() or self.body_ready_reason(refresh=False)
            result['blocked_actions'] = {cmd: reason or
                                          ('身体动作待确认' if self.pending_action else '当前身体姿态不允许此动作')
                                          for cmd in ACTIONS if cmd not in available}
            reasons = self.runtime.life_reasons()
            for action in ('request_rest', 'start_explore'):
                reasons[action] = reasons[action] or self.body_ready_reason(refresh=False)
            result['available_life_actions'] = [key for key, reason in reasons.items() if reason is None]
            result['life_blocked_reasons'] = reasons
            return result

    @property
    def available_actions(self) -> list[str]:
        self._refresh_body_freshness()
        return self._available_actions()

    def _available_actions(self) -> list[str]:
        """Allowed body actions derived from a single committed freshness fact."""
        if self.runtime.active_reason():
            return ['StopMove'] if self.is_connected else []
        if self.pending_action is not None:
            action = ACTIONS[self.pending_action["command"]]
            return ["StopMove"] if action.interruptible and self.fsm_state not in {"offline", "connecting"} else []
        if self.body_ready_reason(refresh=False):
            return ['StopMove'] if self.is_connected else []
        return _FSM_AVAILABLE.get(self.fsm_state, [])

    def fsm_next(self, cmd: str) -> str | None:
        """查询在当前状态下执行 cmd 后的目标状态，无对应转移返回 None。"""
        return _FSM_NEXT.get(self.fsm_state, {}).get(cmd)

    def _fsm_on_progress(self, progress: float) -> None:
        """Record uncorrelated telemetry without completing a body command.

        Sport state does not carry the command request ID. A falling sample may
        belong to an earlier action; the probe records also show progress=0 for
        visibly executed actions. It cannot authorize a dependent action.
        """
        if type(progress) in (int, float) and math.isfinite(progress):
            self._last_progress = progress

    def _fsm_on_posture(self, body_height: object) -> None:
        """Height alone cannot distinguish the real robot's sitting/standing.

        Kept as a callback boundary for older adapters. Completion is handled
        only by the combined fresh LOW/SPORT observation below.
        """

    def _fsm_on_velocity(self, velocity, yaw_speed=None):
        if not self._motion_observation_enabled or self.pending_action or not self.is_connected:
            return
        if not isinstance(velocity, (list, tuple)) or len(velocity) < 3:
            return
        if not all(type(v) in (int, float) and math.isfinite(v) for v in velocity):
            return
        # LF sport velocity is Cartesian x/y/z, not x/y/yaw. The real robot
        # reports a vertical estimator bias even while standing still.
        moving = self._planar_moving(velocity, yaw_speed)
        if moving:
            self._stationary_samples = 0
            self.runtime.body('moving', confirmation='observed')
        else:
            self._stationary_samples += 1
            if self._stationary_samples >= 3:
                self.runtime.body('standing', confirmation='observed')

    @staticmethod
    def _planar_moving(velocity, yaw_speed=None):
        if not isinstance(velocity, (list, tuple)) or len(velocity) < 2:
            return None
        if not all(type(v) in (int, float) and math.isfinite(v) for v in velocity[:2]):
            return None
        turning = type(yaw_speed) in (int, float) and math.isfinite(yaw_speed) and abs(yaw_speed) > .08
        return math.hypot(*velocity[:2]) > .035 or bool(turning)

    def _observed_posture(self, sport, low):
        """Classify physical posture from joint geometry, not the RPC or mode=0.

        Go2 leg order is FR/FL/RR/RL, each with hip/thigh/calf. The sum of
        cosines is the vertical extension in units of one leg segment. This
        separates folded rear legs from a standing body at the same height.
        Thresholds are deliberately separated by an unknown region.
        """
        q = low.get('motor_positions')
        rpy = low.get('imu_rpy')
        height = sport.get('body_height')
        if (not isinstance(q, (list, tuple)) or len(q) != 12
                or not all(type(v) in (int, float) and math.isfinite(v) for v in q)
                or not isinstance(rpy, (list, tuple)) or len(rpy) < 2
                or not all(type(v) in (int, float) and math.isfinite(v) for v in rpy[:2])
                or type(height) not in (int, float) or not math.isfinite(height)
                or not 0 < height < .6
                or self._planar_moving(sport.get('velocity'), sport.get('yaw_speed')) is not False):
            return None
        extension = [math.cos(q[i + 1]) + math.cos(q[i + 1] + q[i + 2]) for i in (0, 3, 6, 9)]
        if height <= .16 and all(value < .9 for value in extension):
            return 'lying'
        if (height >= .17 and abs(rpy[0]) < .3 and -1.2 < rpy[1] < -.35
                and all(value > 1.0 for value in extension[:2])
                and all(value < .7 for value in extension[2:])
                and all(q[index] > 1.6 for index in (7, 10))):
            return 'sitting'
        if abs(rpy[0]) > .3 or abs(rpy[1]) > .4:
            return None
        if height >= .23 and all(1.05 < value < 1.98 for value in extension):
            return 'standing'
        return None

    def _fsm_on_joint_observation(self, now=None):
        now = time.monotonic() if now is None else now
        sport = self._robot_state
        low = self._low_state
        q = low.get('motor_positions')
        if (not isinstance(q, list) or len(q) != 12
                or not all(type(v) in (int, float) and math.isfinite(v) for v in q)):
            return
        sport_at = getattr(self, '_sport_state_observed_at', None)
        if not self.is_connected or sport_at is None or now - sport_at > .5:
            return
        previous = self._last_motor_positions
        self._last_motor_positions = tuple(q)
        self._last_motor_observed_at = now
        self._joint_observation_available = True
        self.runtime.commit('body_telemetry_received', body_telemetry_fresh=True)
        step = max(abs(a - b) for a, b in zip(q, previous)) if previous else math.inf
        candidate = self._observed_posture(sport, low)
        stable = candidate is not None and step < .025
        anchor = self._posture_candidate_positions
        drift = max(abs(a - b) for a, b in zip(q, anchor)) if anchor else math.inf
        if (not stable or candidate != self._posture_candidate or drift > .035
                or self._posture_candidate_since is None):
            self._posture_candidate = candidate if stable else None
            self._posture_candidate_since = now if stable else None
            self._posture_candidate_samples = 1 if stable else 0
            self._posture_candidate_positions = tuple(q) if stable else None
        else:
            self._posture_candidate_samples += 1
        pending = self.pending_action
        observation = self._action_observation
        if pending and observation and pending.get('request_id') == observation['request_id']:
            baseline = observation.get('baseline')
            changed = baseline and max(abs(a - b) for a, b in zip(q, baseline)) > .18
            if changed and not observation['motion_seen']:
                observation['motion_seen'] = True
                self.pending_action = {**pending, 'phase': 'executing', 'motion_observed': True,
                                       'started_at': time.time()}
                pending = self.pending_action
        settle = .8 if pending else .4
        if (self._posture_candidate_since is None or now - self._posture_candidate_since < settle
                or self._posture_candidate_samples < 5):
            return
        if pending:
            if not observation or observation['request_id'] != pending.get('request_id'):
                return
            target = pending.get('target_state')
            if pending.get('phase') in {'timeout', 'cancelled_unconfirmed', 'stop_requested_unconfirmed'}:
                # A confirmed stable posture restores control, but does not
                # change an unconfirmed/cancelled action into a success.
                self._complete_observed_action(candidate, result='cancelled' if
                    pending['phase'] != 'timeout' else 'timeout', phase='recovered')
            elif observation.get('acknowledged') and observation['motion_seen'] and candidate == target:
                self._complete_observed_action(candidate)
            return
        self.runtime.body(candidate, confirmation='observed', source='joint_posture')

    def _complete_observed_action(self, body, *, result='ok', phase='completed'):
        pending = self.pending_action
        if not pending:
            return
        waiter = getattr(self, '_completion', None)
        self.runtime.body(body, confirmation='observed', source='joint_posture')
        self.runtime.finish_body_action(pending['request_id'], result, phase=phase,
                                        confirmation='joint_posture')
        self._action_observation = None
        deadline = getattr(self, '_action_deadline', None)
        if deadline is not None:
            deadline.cancel()
            self._action_deadline = None
        if waiter is not None and not waiter.done():
            waiter.set_result({'result': result, 'confirmation': 'joint_posture',
                               'confirmation_scope': 'physical', 'fsm_state': body,
                               'request_id': pending['request_id']})
