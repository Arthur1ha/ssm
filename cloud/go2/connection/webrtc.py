import asyncio
import json
import logging
import math
import time
import uuid

from unitree_webrtc_connect import WebRTCConnectionMethod
from cloud.go2.connection.remote_signaling import RemoteConnection as UnitreeWebRTCConnection
from unitree_webrtc_connect.constants import RTC_TOPIC, OBSTACLES_AVOID_API, DATA_CHANNEL_TYPE

from cloud.go2.connection.fsm import Go2FSM, CommandBlocked
from cloud.go2.actions import (VELOCITY_LIMITS, ACTIONS, SPORT_COMMANDS, LED_COLORS, LED_DEFAULT_COLOR,
                               LED_DEFAULT_DURATION, LED_MAX_DURATION)
from cloud.go2.connection.sensors import Go2Sensors
from cloud.go2.connection.video import Go2Video
from cloud.go2.connection.manual_velocity import ManualVelocityControl

logger = logging.getLogger(__name__)

_STATUS_INTERVAL  = 10.0
_STATE_STALE_S = 20.0
_SPORT_RPC_TIMEOUT_S = 5.0
_CONNECT_TIMEOUT_S = 45.0
_SETUP_TIMEOUT_S = 5.0
_CLOSING_TASKS: set[asyncio.Task] = set()


class Go2Connection(ManualVelocityControl, Go2FSM, Go2Sensors, Go2Video):
    def __init__(self) -> None:
        super().__init__()
        self._conn: UnitreeWebRTCConnection | None = None
        self.is_connected: bool = False
        self._status_task: asyncio.Task | None = None
        self._command_lock = asyncio.Lock()
        self._completion = None
        self._motion_epoch = 0
        self._last_zero_velocity_epoch = None
        self._velocity_request_id = uuid.uuid4().int & 0x7fffffff
        self._action_deadline = None
        self._velocity_timer = None
        self._init_manual_velocity()
        self._move_control_active = False
        self._velocity_command_active = False
        self._odom_session_id = uuid.uuid4().hex
        self._latest_frame = None
        self._latest_frame_meta = None

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def connect(self, email: str, password: str, serial: str, region: str = "cn") -> None:
        self._stop_manual_velocity()
        self._motion_observation_enabled = False
        self._velocity_command_active = False
        if self._status_task is not None:
            self._status_task.cancel()
            self._status_task = None
        if self._velocity_timer is not None:
            self._velocity_timer.cancel()
            self._velocity_timer = None
        self._finish_pending("cancelled", "reconnecting")
        self._motion_epoch += 1
        self.pending_action = None
        self.body_confirmation = "unknown"
        self._connected_serial = None
        self._latest_frame = None
        self._latest_frame_meta = None
        self._move_control_active = False
        self._velocity_command_active = False
        self._odom_session_id = uuid.uuid4().hex
        self._last_state_received_at = None
        self._sport_state_observed_at = None
        self.reset_sensor_epoch()
        self.runtime.connection("connecting")
        previous, self._conn = self._conn, None
        session_id = self._odom_session_id
        connection = None
        try:
            await self._stop_video()
            if previous is not None:
                await self._close_connection(previous)
            if session_id != self._odom_session_id:
                return
            connection = UnitreeWebRTCConnection(
                WebRTCConnectionMethod.Remote, serialNumber=serial,
                username=email, password=password, region=region, device_type="Go2")
            self._conn = connection
            await asyncio.wait_for(connection.connect(), timeout=_CONNECT_TIMEOUT_S)
            if self._conn is not connection or session_id != self._odom_session_id:
                await self._close_connection(connection)
                return

            # Native decoding returns metric N×3 points, not voxel indices.
            connection.datachannel.set_decoder(decoder_type="native")
            def current(callback):
                def receive(message):
                    if (self._conn is connection and self.is_connected
                            and session_id == self._odom_session_id):
                        callback(message)
                return receive
            for topic, callback in (("LF_SPORT_MOD_STATE", self._on_state),
                                    ("ROBOTODOM", self._on_odom), ("LOW_STATE", self._on_low_state),
                                    ("ULIDAR_ARRAY", self._on_voxel_map),
                                    ("LIDAR_NAVIGATION_GLOBAL_PATH", self._on_global_path)):
                connection.datachannel.pub_sub.subscribe(RTC_TOPIC[topic], current(callback))
            await asyncio.wait_for(connection.datachannel.disableTrafficSaving(True), timeout=_SETUP_TIMEOUT_S)
            if self._conn is not connection or session_id != self._odom_session_id:
                await self._close_connection(connection)
                return
            if self._sport_transport_closed(connection):
                raise ConnectionError('Go2 transport closed during setup')

            self.is_connected = True
            self.body_state = None
            self._motion_observation_enabled = False
            self._connected_serial = serial
            logger.info("[Go2/Conn] WebRTC 连接成功 serial=%s", serial)

            async def transport_changed():
                if (self._conn is connection and session_id == self._odom_session_id
                        and self._sport_transport_closed(connection)):
                    logger.warning("[Go2/Conn] WebRTC transport 已关闭")
                    await self.disconnect()
            for emitter, event in ((connection.pc, 'connectionstatechange'),
                                   (getattr(connection.datachannel, 'channel', None), 'close')):
                register = getattr(emitter, 'on', None)
                if callable(register):
                    register(event, transport_changed)

            video_track = next((t.receiver.track for t in connection.pc.getTransceivers()
                                if t.kind == 'video' and t.receiver and t.receiver.track), None)
            connection.video.switchVideoChannel(True)
            if video_track is not None:
                self._video_task = asyncio.create_task(self._consume_video(video_track), name='go2-video')
                logger.info("[Go2/Conn] 视频 track 已找到，开始采集")
            else:
                self._video_track_ended = True
                self._video_error = 'video_track_missing'
                logger.warning("[Go2/Conn] 未找到视频 track，画面不可用")
            self._last_state_received_at = time.monotonic()
            self._status_task = asyncio.create_task(self._status_loop())
        except (Exception, asyncio.CancelledError) as exc:
            if self._conn is connection and session_id == self._odom_session_id:
                self._conn = None
                self.is_connected = False
                self._connected_serial = None
                self.reset_sensor_epoch()
                await self._stop_video()
            if connection is not None:
                await self._close_connection(connection)
            elif previous is not None:
                await self._close_connection(previous)
            if not isinstance(exc, asyncio.CancelledError):
                logger.error("[Go2/Conn] 连接失败: %s", exc)
            raise

    @staticmethod
    async def _close_connection(connection):
        # aiortc.close() cannot safely be cancelled halfway through: a later
        # close then waits on its unfinished internal Future. Keep the first
        # close alive and observe its result even if its caller is cancelled.
        task = getattr(connection, '_ssm_close_task', None)
        if not isinstance(task, asyncio.Task):
            try:
                task = asyncio.create_task(connection.disconnect(), name='go2-close-peer')
            except Exception as exc:
                logger.warning("[Go2/Conn] 清理 WebRTC 连接失败: %s", type(exc).__name__)
                return
            connection._ssm_close_task = task
            _CLOSING_TASKS.add(task)
            def finished(done):
                _CLOSING_TASKS.discard(done)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (Exception, asyncio.CancelledError):
                pass
            raise
        except Exception as exc:
            logger.warning("[Go2/Conn] 清理 WebRTC 连接失败: %s", type(exc).__name__)


    async def disconnect(self) -> None:
        self._stop_manual_velocity()
        connection, self._conn = self._conn, None
        self._odom_session_id = uuid.uuid4().hex
        owner = self.runtime.data['life_owner']
        self._connected_serial = None
        self._latest_frame = None
        self._latest_frame_meta = None
        self._move_control_active = False
        if self._velocity_timer:
            self._velocity_timer.cancel()
        self._finish_pending("error", "disconnected")
        self._motion_epoch += 1
        self.pending_action = None
        self.body_confirmation = "unknown"
        self._motion_observation_enabled = False
        self._velocity_command_active = False
        self.is_connected = False
        self.runtime.connection("offline")
        self._last_state_received_at = None
        self._sport_state_observed_at = None
        self.reset_sensor_epoch()
        if owner:
            from cloud.go2.control import session_tasks
            session_tasks.cancel(owner)
        # Only the installed device owns the process-wide navigator.
        if self is globals().get('go2'):
            from cloud.go2.navigation.navigator import navigator
            navigator.stop(owner_session_id=owner) if owner else navigator.stop()
        if self._status_task and not self._status_task.done():
            if self._status_task is not asyncio.current_task():
                self._status_task.cancel()
            self._status_task = None
        # Capture the connection before awaiting: a later connect owns its own
        # peer and must never be closed by this retiring coroutine.
        await self._stop_video()
        if connection is not None:
            await self._close_connection(connection)
        logger.info("[Go2/Conn] 已断开")

    # ── 定时状态快照 ──────────────────────────────────────────────────────

    async def _status_loop(self) -> None:
        from cloud.go2.navigation.navigator import navigator as _nav
        connection = self._conn
        session = self._odom_session_id
        while self.is_connected and self._conn is connection and self._odom_session_id == session:
            if self._sport_transport_closed(connection):
                logger.warning("[Go2/Conn] WebRTC transport 已关闭")
                await self.disconnect()
                return
            if (self._last_state_received_at is not None
                    and time.monotonic() - self._last_state_received_at > _STATE_STALE_S):
                logger.warning("[Go2/Conn] 设备遥测中断，标记为离线")
                await self.disconnect()
                return
            odom = self._odom
            low  = self._low_state
            hdg  = math.degrees(odom.get("heading", 0.0))
            batt = low.get("battery_soc")
            batt_str = f"{batt}%" if batt is not None else "?"
            logger.info(
                "[Go2/Status] fsm=%-10s | x=%+.2f y=%+.2f hdg=%+.1f° | batt=%s | nav=%-12s tgt=%s",
                self.body_state,
                odom.get("x", 0.0), odom.get("y", 0.0), hdg,
                batt_str,
                _nav.mode.value,
                _nav.target or "-",
            )
            publisher = getattr(self, "_peer_state_callback", None)
            if publisher is not None:
                publisher()
            await asyncio.sleep(_STATUS_INTERVAL)

    # ── 命令发送 ──────────────────────────────────────────────────────────

    async def read_avoidance_switch(self) -> dict:
        """Read the avoidance switch on an existing session; never change mode."""
        if not self.is_connected or self._conn is None:
            raise RuntimeError("Go2 not connected")
        response = await asyncio.wait_for(
            self._conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["OBSTACLES_AVOID"],
                {"api_id": OBSTACLES_AVOID_API["SWITCH_GET"]}),
            timeout=5.0,
        )
        packet = response.get("data") if isinstance(response, dict) else None
        if not isinstance(packet, dict):
            return {"result": "unknown", "reason": "invalid_switch_response"}
        status = (packet.get("header") or {}).get("status") or {}
        payload = packet.get("data")
        if isinstance(payload, str):
            try:
                import json
                payload = json.loads(payload)
            except ValueError:
                payload = None
        enabled = payload.get("enable") if isinstance(payload, dict) else None
        code = status.get("code") if isinstance(status, dict) else None
        return {"result": "ok" if code == 0 and type(enabled) is bool else "unknown",
                "request_status_code": code, "enabled": enabled if type(enabled) is bool else None,
                "motion_channel": "SPORT_MOD/Move",
                "connection_method": "Remote"}

    def _finish_pending(self, result: str, error: str) -> None:
        pending = self.pending_action
        if pending and error in {'disconnected', 'reconnecting'}:
            self.runtime.commit('body_action_interrupted', last_action={
                **pending, 'phase': 'cancelled' if result == 'cancelled' else 'error',
                'result': result, 'error': error, 'finished_at': time.time()})
        if self._completion is not None and not self._completion.done():
            self._completion.set_result({"result": result, "error": error})
        self._completion = None
        if self._action_deadline is not None:
            self._action_deadline.cancel()
            self._action_deadline = None

    def _expire_body_action(self, request_id):
        pending = self.pending_action
        if not pending or pending.get('request_id') != request_id:
            return
        deadline = self._action_deadline
        self._action_deadline = None
        if deadline is not None:
            deadline.cancel()
        terminal = {**pending, 'phase': 'timeout', 'result': 'timeout',
                    'error': 'completion_not_observed', 'finished_at': time.time(), 'ts': time.time()}
        self.runtime.commit('body_action_timeout', pending_action={**pending, 'phase': 'timeout'},
                            last_action=terminal)
        if self._completion is not None and not self._completion.done():
            self._completion.set_result({
                'result': 'timeout', 'error': 'completion_not_observed',
                'request_id': request_id, 'pending_action': self.pending_action,
                'confirmation_scope': 'request', 'body_state': self.body_state})

    async def _publish_sport_request(self, connection, options):
        """Retire the SDK's resolver when a bounded RPC wait is cancelled.

        The SDK otherwise keeps the cancelled future and a late packet calls
        set_result on it. Only this request's explicit wire identity is removed.
        """
        pub_sub = connection.datachannel.pub_sub
        try:
            return await pub_sub.publish_request_new(RTC_TOPIC['SPORT_MOD'], options)
        finally:
            resolver = getattr(pub_sub, 'future_resolver', None)
            callbacks = getattr(resolver, 'pending_callbacks', None)
            if isinstance(callbacks, dict):
                generate = getattr(resolver, 'generate_message_key', None)
                key = generate(DATA_CHANNEL_TYPE['REQUEST'], RTC_TOPIC['SPORT_MOD'], options['id']) if callable(generate) else options['id']
                callbacks.pop(key, None)

    @staticmethod
    def _sport_transport_closed(connection):
        channel = getattr(getattr(connection, 'datachannel', None), 'channel', None)
        peer = getattr(connection, 'pc', None)
        return (getattr(channel, 'readyState', None) in {'closed', 'closing'}
                or getattr(peer, 'connectionState', None) in {'closed', 'failed', 'disconnected'})

    def _validate_motion(self, cmd: str, params: dict | None = None, *, dispatch_only=False) -> str | None:
        spec = ACTIONS.get(cmd)
        if spec is None:
            raise ValueError(f"Unknown command: {cmd}")
        if not isinstance(params, (dict, type(None))):
            raise ValueError("command params must be an object")
        if set(params or {}) - set(spec.parameter_keys):
            raise ValueError(f"Unsupported parameters for {cmd}")
        if dispatch_only:
            if cmd not in SPORT_COMMANDS or cmd == 'StopMove':
                raise ValueError('dispatch_only requires a discrete body action')
            reason = self.runtime.active_reason()
            if reason:
                raise CommandBlocked(reason)
            return spec.execution_state or spec.expected_state
        if self.pending_action is not None and cmd != "StopMove":
            raise CommandBlocked("body_action_unconfirmed")
        if cmd != "StopMove":
            reason = self.runtime.active_reason() or self.body_ready_reason()
            if reason:
                raise CommandBlocked(reason)
        target = self.fsm_next(cmd)
        # Continuous movement updates and emergency stop are idempotent.
        if cmd == "Move" and self.body_state == "moving":
            target = "moving"
        if cmd == "StopMove" and self.is_connected:
            target = self.body_state if self.body_state in {"standing", "lying", "sitting"} else None
        if cmd == "BalanceStand" and self.body_state == "standing":
            target = "standing"
        if target is None and cmd != "StopMove":
            raise CommandBlocked(f"{cmd} blocked in {self.body_state}")
        if cmd == "Move":
            velocity = {k: (params or {}).get(k, 0.0) for k in ("x", "y", "z")}
            self._validate_velocity(**velocity)
            if not any(velocity.values()):
                raise ValueError("Move requires nonzero velocity; use the joystick to move")
        return target

    @staticmethod
    def _validate_velocity(x=0.0, y=0.0, z=0.0):
        for axis, value in (("x", x), ("y", y), ("z", z)):
            limit = VELOCITY_LIMITS[axis]
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
                raise ValueError(f"{axis} velocity outside supported range (+/-{limit})")

    async def send_command(self, cmd: str, params: dict | None = None, *,
                           completion_timeout: float | None = None, dispatch_only=False) -> dict:
        async with self._command_lock:
            waiter, response = await self._send_command(cmd, params, dispatch_only=dispatch_only)
        spec = ACTIONS[cmd]
        packet = response.get("data") if isinstance(response, dict) else None
        header = packet.get("header") if isinstance(packet, dict) else None
        status = header.get("status") if isinstance(header, dict) else None
        code = status.get("code") if isinstance(status, dict) else None
        if waiter is None:
            if type(code) is int and code != 0:
                return {"result": "error", "error": "sport_rpc_rejected",
                        "request_status_code": code, "body_state": self.body_state,
                        "confirmation_scope": "request"}
            return {"result": "accepted", "confirmation": "sport_rpc" if code == 0 else "transport",
                    "confirmation_scope": "request", "body_state": self.body_state,
                    **({'dispatch_only': True} if dispatch_only else {}),
                    "target_state": spec.expected_state,
                    "completion_supported": not dispatch_only and spec.completion == "joint_posture" and self._joint_observation_available,
                    "pending_action": self.pending_action.copy() if self.pending_action else None,
                    "request_status_code": code}
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=(
                spec.timeout_s if completion_timeout is None else completion_timeout))
        except asyncio.TimeoutError:
            # A deadline only bounds waiting. It is never posture evidence and
            # does not trigger retries or an unverified recovery command.
            if self._completion is waiter and self.pending_action is not None:
                self._expire_body_action(self.pending_action['request_id'])
            return {"result": "timeout", "error": "completion_not_observed",
                    "body_state": self.body_state, "target_state": spec.expected_state,
                    "confirmation_scope": "request", "request_status_code": code,
                    "pending_action": self.pending_action}
        except asyncio.CancelledError:
            if self._completion is waiter and self.pending_action is not None:
                self.pending_action = {**self.pending_action, "phase": "cancelled_unconfirmed"}
            raise
        finally:
            if self._completion is waiter:
                self._completion = None
            if not waiter.done():
                waiter.cancel()

    async def _send_command(self, cmd: str, params: dict | None, *, dispatch_only=False):
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        next_state = self._validate_motion(cmd, params, dispatch_only=dispatch_only)
        spec = ACTIONS[cmd]
        if cmd == "EnterMoveControl":
            # Prepare the explicit Sport Move channel without entering
            # BalanceStand, whose joystick input controls body attitude.
            waiter, response = await self._send_command('StopMove', None)
            if waiter is not None:
                return waiter, response
            packet = response.get('data') if isinstance(response, dict) else None
            header = packet.get('header') if isinstance(packet, dict) else None
            status = header.get('status') if isinstance(header, dict) else None
            code = status.get('code') if isinstance(status, dict) else None
            if type(code) is not int:
                raise ValueError('机器狗尚未确认移动准备')
            if code != 0:
                return None, response
            self._validate_motion(cmd)
            self._move_control_active = True
            logger.info('[Go2/Conn] 移动准备已确认（Sport Move/StopMove）')
            return None, response
        if (self._move_control_active or self._velocity_command_active) and cmd not in {'Move', 'BalanceStand'}:
            # Stop continuous velocity before starting another body action.
            self.stop_velocity()
        source = self.body_state
        source_confirmation = self.body_confirmation
        old_pending = self.pending_action
        self._finish_pending("cancelled", "superseded")
        self._completion = None
        self._motion_epoch += 1
        epoch = self._motion_epoch
        connection = self._conn
        request_id = uuid.uuid4().hex
        wire_id = uuid.uuid4().int & 0x7fffffff or 1
        # Goals are stored separately. In particular, a sent Sit command is not
        # a sitting observation and its high body height is not standing proof.
        body_action = cmd not in {"StopMove", "Move", "BalanceStand"}
        waiter = None
        if body_action:
            self._motion_observation_enabled = False
            self._action_observation = None if dispatch_only else {'request_id': request_id,
                                        'baseline': self._last_motor_positions,
                                        'motion_seen': False, 'acknowledged': False}
            self._posture_candidate_since = None
            self._posture_candidate_samples = 0
            self.pending_action = {"command": cmd, "request_id": request_id,
                                   "rpc_request_id": wire_id,
                                   "label": spec.label,
                                   "target_state": spec.expected_state,
                                   "execution_state": spec.execution_state,
                                   "source_state": source,
                                   "life_task_id": self.runtime.data['life_task_id'],
                                   "sent_at": time.time(), "ts": time.time(), "phase": "sending"}
            self.body_confirmation = "unknown"
            self.body_state = None
            self._move_control_active = False
            if not dispatch_only:
                self._action_deadline = asyncio.get_running_loop().call_later(
                    spec.timeout_s, self._expire_body_action, request_id)
                if spec.completion == 'joint_posture' and self._joint_observation_available:
                    waiter = asyncio.get_running_loop().create_future()
                    self._completion = waiter
        previous_velocity_requested = self._velocity_command_active
        if cmd == 'Move':
            self._velocity_command_active = True
        options = {"api_id": spec.api_id, "id": wire_id}
        if params:
            options["parameter"] = params
        try:
            response = await asyncio.wait_for(
                self._publish_sport_request(connection, options),
                timeout=min(spec.timeout_s, _SPORT_RPC_TIMEOUT_S))
        except asyncio.CancelledError:
            if (epoch == self._motion_epoch and connection is self._conn and body_action
                    and self.pending_action is not None):
                self.pending_action = {**self.pending_action, "phase": "cancelled_unconfirmed"}
            if self._completion is waiter:
                self._completion = None
            if waiter is not None and not waiter.done():
                waiter.cancel()
            raise
        except Exception as exc:
            if epoch != self._motion_epoch or connection is not self._conn:
                cancelled = asyncio.get_running_loop().create_future()
                cancelled.set_result({"result": "cancelled", "error": "superseded_during_send"})
                return cancelled, None
            if self._sport_transport_closed(connection):
                self._finish_pending("error", "disconnected")
                self.is_connected = False
                self.body_confirmation = "unknown"
                raise RuntimeError(f"发送失败，连接已断开: {exc}") from exc
            # RPC receipt is separate from both connection liveness and physical
            # completion. Keep observing the original action; never send again.
            error = 'sport_rpc_timeout' if isinstance(exc, TimeoutError) else 'sport_rpc_failed'
            logger.warning('[Go2/Conn] RPC未确认 cmd=%s request=%s rpc=%s error=%s; 保留遥测观察',
                           cmd, request_id, wire_id, error)
            if waiter is not None:
                return waiter, None
            observed = self.runtime.data.get('last_action') or {}
            if observed.get('request_id') == request_id and observed.get('confirmation') == 'joint_posture':
                completed = asyncio.get_running_loop().create_future()
                completed.set_result({'result': observed['result'], 'request_id': request_id,
                                      'confirmation': 'joint_posture', 'confirmation_scope': 'physical',
                                      'body_state': self.body_state})
                return completed, None
            if dispatch_only:
                self.runtime.finish_body_action(request_id, 'timeout', phase='dispatched_unconfirmed',
                                                error=error)
            unconfirmed = asyncio.get_running_loop().create_future()
            unconfirmed.set_result({'result': 'timeout' if isinstance(exc, TimeoutError) else 'unknown',
                                    'error': error, 'request_id': request_id,
                                    'rpc_request_id': wire_id, 'confirmation_scope': 'request',
                                    'pending_action': self.pending_action})
            return unconfirmed, None
        if epoch != self._motion_epoch or connection is not self._conn or not self.is_connected:
            cancelled = asyncio.get_running_loop().create_future()
            cancelled.set_result({"result": "cancelled", "error": "superseded_during_send"})
            return cancelled, response
        packet = response.get("data") if isinstance(response, dict) else None
        header = packet.get("header") if isinstance(packet, dict) else None
        status = header.get("status") if isinstance(header, dict) else None
        code = status.get("code") if isinstance(status, dict) else None
        # Joint feedback can complete the action while its RPC is still pending.
        # A late receipt must not recreate pending state or replace that result.
        if waiter is not None and waiter.done():
            return waiter, response
        observed = self.runtime.data.get('last_action') or {}
        if observed.get('request_id') == request_id and observed.get('confirmation') == 'joint_posture':
            completed = asyncio.get_running_loop().create_future()
            completed.set_result({'result': observed['result'], 'request_id': request_id,
                                  'confirmation': 'joint_posture', 'confirmation_scope': 'physical',
                                  'body_state': self.body_state})
            return completed, response
        if type(code) is int and code != 0:
            if cmd == 'Move':
                self._velocity_command_active = previous_velocity_requested
            if body_action:
                self._finish_pending('error', 'sport_rpc_rejected')
                self.runtime.finish_body_action(request_id, 'error', phase='rejected',
                                                error='sport_rpc_rejected')
                self._action_observation = None
                self.pending_action = old_pending
                self.body_state = source
                self.body_confirmation = source_confirmation
            return None, response
        if body_action:
            if dispatch_only:
                # A manual button requests a device-native action. Receipt of
                # the request ends this call; it does not prove a posture.
                self._action_observation = None
                self.runtime.finish_body_action(request_id, 'accepted', phase='dispatched',
                                                confirmation='sport_rpc' if code == 0 else 'transport')
                logger.info('[Go2/Conn] 手动命令已发送: %s request=%s code=%s', cmd, request_id, code)
                return None, response
            observation = self._action_observation
            observation['acknowledged'] = True
            self.pending_action = {**self.pending_action,
                                   "phase": "executing" if observation['motion_seen'] else
                                   "accepted" if code == 0 else "sent"}
        elif cmd == "StopMove":
            self._velocity_command_active = False
            self._move_control_active = False
            if self.pending_action is not None:
                self.pending_action = {**self.pending_action, "phase": "stop_requested_unconfirmed"}
            self.body_state = next_state
        elif cmd == "Move":
            self._motion_observation_enabled = True
            self._stationary_samples = 0
        logger.info("[Go2/Conn] 发送命令: %s request=%s code=%s", cmd, request_id, code)
        return waiter, response

    async def switch_mode(self, mode: str) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["MOTION_SWITCHER"],
            {"api_id": 1002, "parameter": {"name": mode}},
        )

    def _publish_velocity(self, vx, vy, vyaw):
        active = any((vx, vy, vyaw))
        try:
            self._velocity_request_id = (self._velocity_request_id + 1) & 0x7fffffff or 1
            self._conn.datachannel.pub_sub.publish_without_callback(
                RTC_TOPIC['SPORT_MOD'],
                data={'header': {'identity': {'id': self._velocity_request_id,
                                             'api_id': ACTIONS['Move' if active else 'StopMove'].api_id}},
                      'parameter': json.dumps({'x': vx, 'y': vy, 'z': vyaw}) if active else ''},
                msg_type=DATA_CHANNEL_TYPE['REQUEST'])
        except Exception as exc:
            self._cancel_manual_velocity()
            self.is_connected = False
            self.runtime.connection('offline')
            raise RuntimeError(f'发送失败，连接已断开: {exc}') from exc

    def move_velocity(self, vx: float, vy: float, vyaw: float, *, deadman_s: float | None = None) -> dict:
        """Stream physical velocities through Sport Move; zero sends StopMove.

        Navigation and manual control use the same m/s and rad/s interface.
        Wireless sticks are mode-dependent and do not implement that contract.
        Requests do not wait for an ACK; odometry proves physical progress.
        """
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        self._validate_velocity(vx, vy, vyaw)
        active = any((vx, vy, vyaw))
        target = self._validate_motion(
            "Move" if active else "StopMove", {"x": vx, "y": vy, "z": vyaw} if active else None,
        )
        if self._command_lock.locked() and any((vx, vy, vyaw)):
            raise CommandBlocked("command_in_flight")
        self._cancel_manual_velocity()
        self._publish_velocity(vx, vy, vyaw)
        self._finish_pending("cancelled", "velocity_override")
        self._motion_epoch += 1
        self._velocity_command_active = active
        self._last_zero_velocity_epoch = self._motion_epoch if not active else None
        if active:
            self._motion_observation_enabled = True
            self._stationary_samples = 0
        elif self.body_state == "moving":
            self.runtime.body(None, confirmation="unknown")
        self._state_changed()
        if self._velocity_timer:
            self._velocity_timer.cancel()
            self._velocity_timer = None
        if deadman_s and any((vx, vy, vyaw)):
            self._velocity_timer = asyncio.get_running_loop().call_later(deadman_s, self._expire_velocity)
        return {"result": "accepted", "confirmation": "transport", "body_state": target}

    def stop_velocity(self) -> dict:
        """紧急停止和会话清理同时退出遥控状态。"""
        stopped_state = self.body_state if self.body_state in {"standing", "lying", "sitting"} else None
        result = self.move_velocity(0, 0, 0)
        self._move_control_active = False
        self.runtime.commit("remote_exit", control_mode="normal")
        self.body_state = stopped_state
        result["body_state"] = stopped_state
        return result

    def _expire_velocity(self):
        self._velocity_timer = None
        try:
            self.move_velocity(0, 0, 0)
        except (RuntimeError, ValueError):
            logger.warning("velocity stop could not be sent")

    async def balance_stand(self) -> None:
        return await self.send_command("BalanceStand")

    async def set_led(self, color: str = LED_DEFAULT_COLOR, duration: int = LED_DEFAULT_DURATION) -> None:
        if type(duration) is not int or not 1 <= duration <= LED_MAX_DURATION:
            raise ValueError(f"duration must be in [1, {LED_MAX_DURATION}]")
        if color not in LED_COLORS:
            raise ValueError(
                f"Unknown color {color!r}, choose from: {', '.join(sorted(LED_COLORS))}"
            )
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["VUI"],
            {"api_id": 1001, "parameter": {"color": color, "time": duration}},
        )


go2 = Go2Connection()
