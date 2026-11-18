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
from cloud.go2.motion import COMMAND_TTL_S

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
        self._avoidance_lock = asyncio.Lock()
        self._avoidance_restore_expected = None
        self._classic_walk_connection = None
        self._native_motion_connection = None
        self._native_motion_mode = None
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
        self.is_connected = False
        self._classic_walk_connection = None
        self._native_motion_connection = None
        self._native_motion_mode = None
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
        self._classic_walk_connection = None
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

    async def read_motion_mode(self) -> dict:
        """Read the controller name; SportModeState.mode is a separate field."""
        connection = self._conn
        if not self.is_connected or connection is None:
            raise RuntimeError('Go2 not connected')
        response = await asyncio.wait_for(connection.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC['MOTION_SWITCHER'], {'api_id': 1001}), timeout=_SETUP_TIMEOUT_S)
        if self._conn is not connection or not self.is_connected:
            raise ValueError('motion_mode_connection_changed')
        packet = response.get('data') if isinstance(response, dict) else None
        header = packet.get('header') if isinstance(packet, dict) else None
        status = header.get('status') if isinstance(header, dict) else None
        code = status.get('code') if isinstance(status, dict) else None
        payload = packet.get('data') if isinstance(packet, dict) else None
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        name = payload.get('name') if isinstance(payload, dict) else None
        confirmed = type(code) is int and code == 0 and isinstance(name, str) and bool(name)
        if confirmed:
            self._native_motion_mode = name
        logger.info('[Go2/MotionMode] query code=%s name=%s confirmed=%s', code, name, confirmed)
        return {'result': 'ok' if confirmed else 'unknown', 'name': name if confirmed else None,
                'request_status_code': code}

    async def read_motion_settings(self) -> dict:
        """Read native settings, without preparing a gait or sending motion.

        MCF's LF SportModeState may contain placeholder gait/foot fields. Keep
        GetState's raw settings separate from measured physical gait evidence.
        """
        connection = self._conn
        if not self.is_connected or connection is None:
            raise RuntimeError('Go2 not connected')
        response = await asyncio.wait_for(self._publish_sport_request(connection, {
            'api_id': 1034, 'id': time.time_ns() & 0x7fffffff or 1,
            'parameter': ['state', 'bodyHeight', 'speedLevel', 'gait',
                          'continuousGait', 'economicGait'],
        }), timeout=_SPORT_RPC_TIMEOUT_S)
        if self._conn is not connection or not self.is_connected:
            raise ValueError('motion_settings_connection_changed')
        packet = response.get('data') if isinstance(response, dict) else None
        header = packet.get('header') if isinstance(packet, dict) else None
        status = header.get('status') if isinstance(header, dict) else None
        code = status.get('code') if isinstance(status, dict) else None
        payload = packet.get('data') if isinstance(packet, dict) else None
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        confirmed = type(code) is int and code == 0 and isinstance(payload, dict) and bool(payload)
        logger.info('[Go2/MotionSettings] code=%s settings=%s', code, payload)
        return {'result': 'ok' if confirmed else 'unknown',
                'request_status_code': code, 'settings': payload if confirmed else None,
                'confirmation_scope': 'settings_only'}

    async def _ensure_native_motion_mode(self, connection, epoch):
        if self._native_motion_connection is connection:
            return

        def current():
            if (self._conn is not connection or not self.is_connected or self._motion_epoch != epoch):
                raise CommandBlocked('motion_mode_owner_changed')

        current()
        mode = await self.read_motion_mode()
        current()
        if mode.get('result') != 'ok':
            raise CommandBlocked('原生运动控制模式尚未确认')
        # MCF firmware reports "mcf" and uses the shared Move/StopMove API
        # directly. Forcing it through the legacy normal-mode handshake is
        # unsupported and blocks otherwise valid movement.
        if mode['name'] not in {'normal', 'mcf'}:
            if mode['name'] != 'ai':
                raise CommandBlocked(f"不支持的原生运动模式: {mode['name']}")
            await self.switch_mode('normal')
            current()
            deadline = time.monotonic() + _SETUP_TIMEOUT_S
            while time.monotonic() < deadline:
                mode = await self.read_motion_mode()
                current()
                if mode.get('result') == 'ok' and mode['name'] == 'normal':
                    break
                await asyncio.sleep(.1)
            else:
                raise CommandBlocked('原生普通运动模式尚未确认')
        self._native_motion_connection = connection

    async def read_avoidance_switch(self) -> dict:
        """Read the avoidance switch on an existing session; never change mode."""
        if not self.is_connected or self._conn is None:
            raise RuntimeError("Go2 not connected")
        connection = self._conn
        response = await asyncio.wait_for(
            connection.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["OBSTACLES_AVOID"],
                {"api_id": OBSTACLES_AVOID_API["SWITCH_GET"]}),
            timeout=5.0,
        )
        if self._conn is not connection or not self.is_connected:
            raise ValueError('avoidance_connection_changed')
        packet = response.get("data") if isinstance(response, dict) else None
        if not isinstance(packet, dict):
            return {"result": "unknown", "reason": "invalid_switch_response"}
        header = packet.get('header')
        status = header.get('status') if isinstance(header, dict) else None
        payload = packet.get("data")
        if isinstance(payload, str):
            try:
                import json
                payload = json.loads(payload)
            except ValueError:
                payload = None
        enabled = payload.get("enable") if isinstance(payload, dict) else None
        code = status.get("code") if isinstance(status, dict) else None
        return {"result": "ok" if type(code) is int and code == 0 and type(enabled) is bool else "unknown",
                "request_status_code": code, "enabled": enabled if type(enabled) is bool else None,
                "motion_channel": "SPORT_MOD/Move",
                "connection_method": "Remote"}

    async def set_avoidance_switch(self, enabled: bool, *, connection) -> dict:
        """Set and read back a switch only on the caller's original connection."""
        if type(enabled) is not bool:
            raise ValueError('invalid_avoidance_switch')
        async with self._avoidance_lock:
            if self._conn is not connection or not self.is_connected:
                raise ValueError('avoidance_connection_changed')
            response = await asyncio.wait_for(
                connection.datachannel.pub_sub.publish_request_new(
                    RTC_TOPIC['OBSTACLES_AVOID'],
                    {'api_id': OBSTACLES_AVOID_API['SWITCH_SET'],
                     'parameter': {'enable': enabled}}), timeout=5.)
            if self._conn is not connection or not self.is_connected:
                raise ValueError('avoidance_connection_changed')
            packet = response.get('data') if isinstance(response, dict) else None
            header = packet.get('header') if isinstance(packet, dict) else None
            status = header.get('status') if isinstance(header, dict) else None
            code = status.get('code') if isinstance(status, dict) else None
            if type(code) is not int or code != 0:
                raise ValueError('avoidance_switch_rejected')
            actual = await self.read_avoidance_switch()
            if actual.get('result') != 'ok' or actual.get('enabled') is not enabled:
                raise ValueError('avoidance_switch_unconfirmed')
            logger.info('[Go2/Avoidance] enabled=%s confirmed=true', enabled)
            return actual

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
            last = self.pending_action or self.runtime.data.get('last_action') or {}
            return {"result": "accepted", "confirmation": "sport_rpc" if code == 0 else "transport",
                    "confirmation_scope": "request", "body_state": self.body_state,
                    **({'request_id': last.get('request_id')} if dispatch_only else {}),
                    **({'dispatch_only': True} if dispatch_only else {}),
                    "target_state": spec.expected_state,
                    "completion_supported": spec.completion == "joint_posture" and self._joint_observation_available,
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
        if cmd == 'Move' and sum(abs((params or {}).get(axis, 0.)) > 1e-9
                                 for axis in ('x', 'y', 'z')) > 1:
            # Task/training dispatch cannot reintroduce mixed-axis walking.
            # Joystick targets use their separate explicit velocity entrance.
            raise CommandBlocked('mixed_motion_command')
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
            await self._ensure_classic_walk()
            self._validate_motion(cmd)
            self._move_control_active = True
            logger.info('[Go2/Conn] 移动准备已确认（Sport Move/StopMove）')
            return None, response
        if cmd == 'Move':
            await self._ensure_classic_walk()
            self._validate_motion(cmd, params)
            # The admitted first frame uses the same no-reply writer as every
            # subsequent navigation/joystick frame. Never wait for a Move RPC
            # before starting a stream, or leave an unbounded one-shot speed.
            self._write_velocity(*((params or {}).get(axis, 0.) for axis in ('x', 'y', 'z')),
                                 source='command', deadman_s=COMMAND_TTL_S)
            return None, None
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
        body_action = cmd not in {"StopMove", "BalanceStand"}
        waiter = None
        if body_action:
            self._motion_observation_enabled = False
            self._action_observation = {'request_id': request_id,
                                        'baseline': self._last_motor_positions,
                                        'after_motor_at': self._low_state_received_at,
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
            if not dispatch_only or self._joint_observation_available:
                self._action_deadline = asyncio.get_running_loop().call_later(
                    spec.timeout_s, self._expire_body_action, request_id)
            if spec.completion == 'joint_posture' and self._joint_observation_available:
                waiter = asyncio.get_running_loop().create_future()
                self._completion = waiter
        options = {"api_id": spec.api_id, "id": wire_id}
        if params:
            options["parameter"] = params
        try:
            # Physical completion may arrive before (or without) the RPC ACK.
            # Do not hold the caller/command lock until the ACK timeout then.
            rpc_task = asyncio.create_task(self._publish_sport_request(connection, options))
            try:
                if waiter is None:
                    response = await asyncio.wait_for(rpc_task, timeout=min(spec.timeout_s, _SPORT_RPC_TIMEOUT_S))
                else:
                    done, _ = await asyncio.wait({rpc_task, waiter},
                        timeout=min(spec.timeout_s, _SPORT_RPC_TIMEOUT_S),
                        return_when=asyncio.FIRST_COMPLETED)
                    if waiter in done:
                        return waiter, None
                    if rpc_task not in done:
                        raise TimeoutError
                    response = rpc_task.result()
            finally:
                if not rpc_task.done():
                    rpc_task.cancel()
                await asyncio.gather(rpc_task, return_exceptions=True)
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
                self._action_observation = None
                self.runtime.finish_body_action(request_id, 'timeout', phase='dispatched_unconfirmed',
                                                error=error)
            unconfirmed = asyncio.get_running_loop().create_future()
            unconfirmed.set_result({'result': 'timeout' if isinstance(exc, TimeoutError) else 'unknown',
                                    'error': error, 'request_id': request_id,
                                    'rpc_request_id': wire_id, 'confirmation_scope': 'request',
                                    **({'dispatch_only': True, 'completion_supported': self._joint_observation_available}
                                       if dispatch_only else {}),
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
                if waiter is not None:
                    # Manual admission does not require a known starting
                    # posture, but available joints still confirm completion.
                    self.pending_action = {**self.pending_action, 'phase': 'executing'
                        if self._action_observation['motion_seen'] else 'accepted' if code == 0 else 'sent'}
                    return waiter, response
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
        logger.info("[Go2/Conn] 发送命令: %s request=%s code=%s", cmd, request_id, code)
        return waiter, response

    async def prepare_motion(self) -> None:
        """Prepare direct navigation streams without issuing a movement."""
        async with self._command_lock:
            params = {'x': .1, 'y': 0., 'z': 0.}
            self._validate_motion('Move', params)
            await self._ensure_classic_walk()
            self._validate_motion('Move', params)

    async def _ensure_classic_walk(self) -> None:
        """Select once when the observed body is ready, shared by all Move users."""
        connection, epoch = self._conn, self._motion_epoch
        await self._ensure_native_motion_mode(connection, epoch)
        if self._avoidance_restore_expected is not None:
            await self.set_avoidance_switch(self._avoidance_restore_expected, connection=connection)
            self._avoidance_restore_expected = None
        if self._native_motion_mode == 'normal':
            # ClassicWalk 2049 is an MCF command, absent from SPORT_CMD's
            # legacy catalog. Keep that controller's own gait and shared Move
            # contract instead of treating an unsupported gait RPC as a
            # universal prerequisite for translation or joystick preparation.
            return
        if self._classic_walk_connection is connection:
            return
        result = await self._select_classic_walk(connection, can_continue=lambda: (
            self.is_connected and self._conn is connection and self._motion_epoch == epoch))
        if result['result'] != 'accepted':
            raise CommandBlocked(f"经典行走步态尚未确认: {result}")

    async def enable_classic_walk(self, *, can_continue) -> dict:
        """Select native ClassicWalk, without changing posture or commanding speed."""
        async with self._command_lock:
            if not can_continue():
                raise ValueError('control_owner_changed')
            self._validate_motion('Move', {'x': .3, 'y': 0., 'z': 0.})
            await self._ensure_native_motion_mode(self._conn, self._motion_epoch)
            if not can_continue():
                raise ValueError('control_owner_changed')
            if self._native_motion_mode != 'mcf':
                return {'result': 'blocked', 'error': 'classic_walk_requires_mcf',
                        'native_motion_mode': self._native_motion_mode,
                        'confirmation_scope': 'request'}
            return await self._select_classic_walk(self._conn, can_continue=can_continue)

    async def _select_classic_walk(self, connection, *, can_continue) -> dict:
        """Configure native gait only; connection setup needs no body posture."""
        if self._conn is not connection or not can_continue():
            raise ValueError('control_owner_changed')
        wire_id = time.time_ns() & 0x7fffffff or 1
        response = await asyncio.wait_for(self._publish_sport_request(connection, {
            'api_id': 2049, 'id': wire_id, 'parameter': {'data': True},
        }), timeout=_SPORT_RPC_TIMEOUT_S)
        packet = response.get('data') if isinstance(response, dict) else None
        header = packet.get('header') if isinstance(packet, dict) else None
        status = header.get('status') if isinstance(header, dict) else None
        code = status.get('code') if isinstance(status, dict) else None
        logger.info('[Go2/Gait] ClassicWalk mode=%s api=2049 enabled=true code=%s request=%s',
                    self._native_motion_mode, code, wire_id)
        if not can_continue() or self._conn is not connection:
            return {'result': 'blocked', 'error': 'control_owner_changed', 'code': code}
        if type(code) is int and code == 0:
            self._classic_walk_connection = connection
        return {'result': 'accepted' if type(code) is int and code == 0 else 'error',
                'gait': 'ClassicWalk', 'api_id': 2049, 'code': code,
                'confirmation_scope': 'request'}

    async def switch_mode(self, mode: str) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        connection = self._conn
        response = await asyncio.wait_for(connection.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["MOTION_SWITCHER"],
            {"api_id": 1002, "parameter": {"name": mode}},
        ), timeout=_SETUP_TIMEOUT_S)
        if self._conn is not connection or not self.is_connected:
            raise ValueError('motion_mode_connection_changed')
        packet = response.get('data') if isinstance(response, dict) else None
        header = packet.get('header') if isinstance(packet, dict) else None
        status = header.get('status') if isinstance(header, dict) else None
        code = status.get('code') if isinstance(status, dict) else None
        logger.info('[Go2/MotionMode] switch target=%s code=%s', mode, code)
        if type(code) is not int or code != 0:
            raise CommandBlocked(f'原生运动模式切换未确认: target={mode} code={code}')
        self._native_motion_connection = self._classic_walk_connection = None
        self._native_motion_mode = None

    def _publish_velocity(self, vx, vy, vyaw, *, source):
        active = any((vx, vy, vyaw))
        try:
            self._velocity_request_id = (self._velocity_request_id + 1) & 0x7fffffff or 1
            header = {'identity': {'id': self._velocity_request_id,
                                  'api_id': ACTIONS['Move' if active else 'StopMove'].api_id}}
            # Not awaiting a Python callback does not make an RPC no-reply.
            # Match SportClient.Move / the MCF WebRTC streaming wire format.
            packet = {'header': header,
                      'parameter': json.dumps({'x': vx, 'y': vy, 'z': vyaw}) if active else ''}
            if active:
                header['policy'] = {'priority': 0, 'noreply': True}
                packet['binary'] = []
            self._conn.datachannel.pub_sub.publish_without_callback(
                RTC_TOPIC['SPORT_MOD'],
                data=packet,
                msg_type=DATA_CHANNEL_TYPE['MSG' if active else 'REQUEST'])
            now = time.monotonic()
            previous = self._last_velocity_packet
            stats = self._motion_command_stats
            stats['move_count' if active else 'stop_count'] += 1
            if active and previous and previous['active']:
                stats['max_gap_s'] = max(stats['max_gap_s'], round(now-previous['sent_at'], 4))
            from cloud.observability import current_event_context, emit_agent_event
            context = current_event_context()
            identity = {key: context[key] for key in ('run_id', 'step_id', 'task_id') if context.get(key)}
            self._last_velocity_packet = {'vx': vx, 'vy': vy, 'yaw': vyaw,
                                          'sent_at': now, 'active': active,
                                          'source': source, **identity}
            if (previous is None or previous['active'] != active
                    or active and (previous.get('source'), previous.get('run_id'), previous.get('step_id'))
                    != (source, context.get('run_id'), context.get('step_id'))):
                emit_agent_event('navigation.progress', phase='velocity_stream',
                    source=source, command={'vx': vx, 'vy': vy, 'yaw': vyaw},
                    active=active, **identity)
                logger.info('[Go2/Velocity] source=%s active=%s run=%s step=%s vx=%.3f vy=%.3f yaw=%.3f',
                    source, active, context.get('run_id'), context.get('step_id'), vx, vy, vyaw)
        except Exception as exc:
            self._cancel_manual_velocity()
            self.is_connected = False
            self.runtime.connection('offline')
            raise RuntimeError(f'发送失败，连接已断开: {exc}') from exc

    def move_velocity(self, vx: float, vy: float, vyaw: float, *, deadman_s: float | None = None,
                      source='velocity') -> dict:
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
        self._write_velocity(vx, vy, vyaw, source=source, deadman_s=deadman_s)
        return {"result": "accepted", "confirmation": "transport", "body_state": target}

    def _write_velocity(self, vx, vy, vyaw, *, source, deadman_s=None, manual_tick=False):
        """Single physical writer after the caller's ownership/body checks.

        Joystick resampling shares motion state and wire output but must never
        renew its user-input deadline. All other writes retire that resampler.
        This method does not await and cannot interleave two physical writes.
        """
        active = any((vx, vy, vyaw))
        if not manual_tick:
            self._cancel_manual_velocity()
        self._publish_velocity(vx, vy, vyaw, source=source)
        self._finish_pending("cancelled", "velocity_override")
        self._motion_epoch += 1
        was_active = self._velocity_command_active
        self._velocity_command_active = active
        self._last_zero_velocity_epoch = self._motion_epoch if not active else None
        if active:
            self._motion_observation_enabled = True
            self._stationary_samples = 0
        elif self.body_state == "moving":
            self.runtime.body(None, confirmation="unknown")
        # A velocity refresh is not a new public state. Telemetry already
        # publishes real body changes; avoid running all state subscribers on
        # every control tick (as with the joystick's periodic stream).
        if active != was_active:
            self._state_changed()
        if not manual_tick and self._velocity_timer:
            self._velocity_timer.cancel()
            self._velocity_timer = None
        if not manual_tick and deadman_s and active:
            self._velocity_timer = asyncio.get_running_loop().call_later(deadman_s, self._expire_velocity)

    def stop_velocity(self) -> dict:
        """紧急停止和会话清理同时退出遥控状态。"""
        stopped_state = self.body_state if self.body_state in {"standing", "lying", "sitting"} else None
        result = self.move_velocity(0, 0, 0)
        self._move_control_active = False
        self.runtime.commit("remote_exit", control_mode="normal")
        self.body_state = stopped_state
        result["body_state"] = stopped_state
        return result

    async def stop_and_confirm_motion(self, *, timeout_s: float = 2.0, can_continue=None,
                                      preserve_remote_control: bool = False) -> dict:
        """Confirm a stop from new measured linear and angular velocities.

        This evidence is independent of posture and RPC acceptance. It neither
        invents standing nor completes another action's correlated receipt.
        A joystick handoff stops motion without exiting an explicitly entered
        remote mode; it must never enable that mode on its own.
        """
        if not self.is_connected:
            return {'result': 'blocked', 'physical_stop': 'unknown', 'reason': 'disconnected'}
        started = time.monotonic()
        self._motion_stationary_samples = 0
        if preserve_remote_control:
            self.move_velocity(0, 0, 0, source='handoff')
        else:
            self.stop_velocity()
        epoch, connection = self._motion_epoch, self._conn
        while time.monotonic() - started < timeout_s:
            if can_continue is not None and not can_continue():
                return {'result': 'blocked', 'physical_stop': 'unknown', 'reason': 'control_owner_changed'}
            if not self.is_connected or self._conn is not connection:
                return {'result': 'blocked', 'physical_stop': 'unknown', 'reason': 'disconnected'}
            if self._motion_epoch != epoch:
                return {'result': 'blocked', 'physical_stop': 'unknown', 'reason': 'motion_changed'}
            if (getattr(self, '_motion_stationary_samples', 0) >= 3
                    and (self._sport_state_observed_at or 0) > started
                    and not self.pending_action and not self._command_lock.locked()):
                return {'result': 'ok', 'physical_stop': 'confirmed',
                        'confirmation': 'fresh_velocity_samples'}
            await asyncio.sleep(.05)
        return {'result': 'blocked', 'physical_stop': 'unknown', 'reason': 'stop_unconfirmed'}

    def _expire_velocity(self):
        self._velocity_timer = None
        logger.warning('[Go2/Motion] velocity input watchdog expired; sending StopMove')
        try:
            self.move_velocity(0, 0, 0, source='watchdog')
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
