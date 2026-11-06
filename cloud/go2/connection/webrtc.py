import asyncio
import logging
import math
import time
import uuid

from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, OBSTACLES_AVOID_API

from cloud.go2.connection.fsm import Go2FSM, CommandBlocked
from cloud.go2.actions import ACTIONS
from cloud.go2.connection.sensors import Go2Sensors
from cloud.go2.connection.video import Go2Video

logger = logging.getLogger(__name__)

_VALID_LED_COLORS = frozenset({"white", "red", "yellow", "blue", "green", "cyan", "purple"})
_STATUS_INTERVAL  = 10.0
_STATE_STALE_S = 20.0


class Go2Connection(Go2FSM, Go2Sensors, Go2Video):
    def __init__(self) -> None:
        super().__init__()
        self._conn: UnitreeWebRTCConnection | None = None
        self.is_connected: bool = False
        self._status_task: asyncio.Task | None = None
        self._command_lock = asyncio.Lock()
        self._completion = None
        self._completion_target: str | None = None
        self._completion_samples = 0
        self._motion_epoch = 0
        self._action_deadline = None
        self._velocity_timer = None
        self._move_control_active = False
        self._velocity_command_active = False
        self._odom_session_id = uuid.uuid4().hex
        self._latest_frame = None
        self._latest_frame_meta = None

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def connect(self, email: str, password: str, serial: str, region: str = "cn") -> None:
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
        self.fsm_state = "connecting"
        if self._conn:
            await self._conn.disconnect()
            self.reset_sensor_epoch()

        self._conn = UnitreeWebRTCConnection(
            WebRTCConnectionMethod.Remote,
            serialNumber=serial,
            username=email,
            password=password,
            region=region,
            device_type="Go2",
        )

        connection = self._conn
        session_id = self._odom_session_id
        try:
            await connection.connect()
        except Exception as exc:
            if self._conn is not connection or session_id != self._odom_session_id:
                return
            self.is_connected = False
            self.fsm_state = "offline"
            logger.error("[Go2/Conn] 连接失败: %s", exc)
            raise

        if self._conn is not connection or session_id != self._odom_session_id:
            await connection.disconnect()
            return

        # Native 解码器返回米制点云（ndarray N×3），比 LibVoxel 的格索引更易过滤地板
        self._conn.datachannel.set_decoder(decoder_type="native")

        connection = self._conn
        def current(callback):
            def receive(message):
                if self._conn is connection and self.is_connected:
                    callback(message)
            return receive
        for topic, callback in (("LF_SPORT_MOD_STATE", self._on_state),
                                ("ROBOTODOM", self._on_odom), ("LOW_STATE", self._on_low_state),
                                ("ULIDAR_ARRAY", self._on_voxel_map),
                                ("LIDAR_NAVIGATION_GLOBAL_PATH", self._on_global_path)):
            self._conn.datachannel.pub_sub.subscribe(RTC_TOPIC[topic], current(callback))
        await connection.datachannel.disableTrafficSaving(True)
        if self._conn is not connection or session_id != self._odom_session_id:
            return

        self.is_connected = True
        self.fsm_state = None
        self._motion_observation_enabled = False
        logger.info("[Go2/Conn] WebRTC 连接成功 serial=%s", serial)
        self._connected_serial = serial

        video_track = None
        for t in self._conn.pc.getTransceivers():
            if t.kind == "video" and t.receiver and t.receiver.track:
                video_track = t.receiver.track
                break

        self._conn.video.switchVideoChannel(True)

        if video_track:
            asyncio.create_task(self._consume_video(video_track))
            logger.info("[Go2/Conn] 视频 track 已找到，开始采集")
        else:
            logger.warning("[Go2/Conn] 未找到视频 track，画面不可用")

        self._last_state_received_at = time.monotonic()
        self._status_task = asyncio.create_task(self._status_loop())


    async def disconnect(self) -> None:
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
        self.fsm_state = "offline"
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
        self._frame_ready = None
        if self._conn:
            try:
                if hasattr(self._conn, "pc") and self._conn.pc:
                    await self._conn.pc.close()
            except Exception:
                pass
            try:
                await self._conn.disconnect()
            except Exception:
                pass
            self._conn = None
        self.reset_sensor_epoch()
        logger.info("[Go2/Conn] 已断开")

    # ── 定时状态快照 ──────────────────────────────────────────────────────

    async def _status_loop(self) -> None:
        from cloud.go2.navigation.navigator import navigator as _nav
        while self.is_connected:
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
                self.fsm_state,
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
                "motion_channel": "WIRELESS_CONTROLLER", "connection_method": "Remote"}

    def _finish_pending(self, result: str, error: str) -> None:
        pending = self.pending_action
        if pending and error in {'disconnected', 'reconnecting'}:
            self.runtime.commit('body_action_interrupted', last_action={
                **pending, 'phase': 'cancelled' if result == 'cancelled' else 'error',
                'result': result, 'error': error, 'finished_at': time.time()})
        if self._completion is not None and not self._completion.done():
            self._completion.set_result({"result": result, "error": error})
        self._completion = None
        self._completion_target = None
        self._completion_samples = 0
        if self._action_deadline is not None:
            self._action_deadline.cancel()
            self._action_deadline = None

    def _expire_body_action(self, request_id):
        deadline = self._action_deadline
        self._action_deadline = None
        if deadline is not None:
            deadline.cancel()
        pending = self.pending_action
        if pending and pending.get('request_id') == request_id:
            terminal = {**pending, 'phase': 'timeout', 'result': 'timeout',
                        'error': 'completion_not_observed', 'finished_at': time.time(), 'ts': time.time()}
            self.runtime.commit('body_action_timeout', pending_action={**pending, 'phase': 'timeout'},
                                last_action=terminal)

    def _validate_motion(self, cmd: str, params: dict | None = None) -> str | None:
        spec = ACTIONS.get(cmd)
        if spec is None:
            raise ValueError(f"Unknown command: {cmd}")
        if not isinstance(params, (dict, type(None))):
            raise ValueError("command params must be an object")
        if set(params or {}) - set(spec.parameter_keys):
            raise ValueError(f"Unsupported parameters for {cmd}")
        if self.pending_action is not None and cmd != "StopMove":
            raise CommandBlocked("body_action_unconfirmed")
        if cmd != "StopMove":
            reason = self.runtime.active_reason() or self.body_ready_reason()
            if reason:
                raise CommandBlocked(reason)
        target = self.fsm_next(cmd)
        # Continuous movement updates and emergency stop are idempotent.
        if cmd == "Move" and self.fsm_state == "moving":
            target = "moving"
        if cmd == "StopMove" and self.fsm_state not in ("offline", "connecting"):
            target = self.fsm_state if self.fsm_state in {"standing", "lying", "sitting"} else None
        if cmd == "BalanceStand" and self.fsm_state == "standing":
            target = "standing"
        if target is None and cmd != "StopMove":
            raise CommandBlocked(f"{cmd} blocked in {self.fsm_state}")
        if cmd == "Move":
            velocity = {k: (params or {}).get(k, 0.0) for k in ("x", "y", "z")}
            self._validate_velocity(**velocity)
            if not any(velocity.values()):
                raise ValueError("Move requires nonzero velocity; use the joystick to move")
        return target

    @staticmethod
    def _validate_velocity(x=0.0, y=0.0, z=0.0):
        for value, limit in ((x, 0.5), (y, 0.5), (z, 0.8)):
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
                raise ValueError("velocity outside supported range (0.5 m/s, 0.8 rad/s)")

    async def send_command(self, cmd: str, params: dict | None = None, *,
                           completion_timeout: float | None = None) -> dict:
        async with self._command_lock:
            waiter, response = await self._send_command(cmd, params)
        spec = ACTIONS[cmd]
        packet = response.get("data") if isinstance(response, dict) else None
        header = packet.get("header") if isinstance(packet, dict) else None
        status = header.get("status") if isinstance(header, dict) else None
        code = status.get("code") if isinstance(status, dict) else None
        if waiter is None:
            if type(code) is int and code != 0:
                return {"result": "error", "error": "sport_rpc_rejected",
                        "request_status_code": code, "fsm_state": self.fsm_state,
                        "confirmation_scope": "request"}
            return {"result": "accepted", "confirmation": "sport_rpc" if code == 0 else "transport",
                    "confirmation_scope": "request", "fsm_state": self.fsm_state,
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
                    "fsm_state": self.fsm_state, "target_state": spec.expected_state,
                    "confirmation_scope": "request", "request_status_code": code}
        except asyncio.CancelledError:
            if self._completion is waiter and self.pending_action is not None:
                self.pending_action = {**self.pending_action, "phase": "cancelled_unconfirmed"}
            raise
        finally:
            if self._completion is waiter:
                self._completion = None
                self._completion_target = None
                self._completion_samples = 0
            if not waiter.done():
                waiter.cancel()

    async def _send_command(self, cmd: str, params: dict | None):
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        next_state = self._validate_motion(cmd, params)
        spec = ACTIONS[cmd]
        if cmd == "EnterMoveControl":
            self._move_control_active = True
            self.runtime.commit("remote_enter", control_mode="remote")
            return None, None
        source = self.fsm_state
        old_pending = self.pending_action
        self._finish_pending("cancelled", "superseded")
        self._completion = None
        self._completion_target = None
        self._completion_samples = 0
        self._motion_epoch += 1
        epoch = self._motion_epoch
        connection = self._conn
        request_id = uuid.uuid4().hex
        # Goals are stored separately. In particular, a sent Sit command is not
        # a sitting observation and its high body height is not standing proof.
        body_action = cmd not in {"StopMove", "Move", "BalanceStand"}
        waiter = None
        if body_action:
            self._motion_observation_enabled = False
            self._action_observation = {'request_id': request_id,
                                        'baseline': self._last_motor_positions,
                                        'motion_seen': False, 'acknowledged': False}
            self._posture_candidate_since = None
            self._posture_candidate_samples = 0
            self.pending_action = {"command": cmd, "request_id": request_id,
                                   "label": spec.label,
                                   "target_state": spec.expected_state,
                                   "execution_state": spec.execution_state,
                                   "source_state": source,
                                   "life_task_id": self.runtime.data['life_task_id'],
                                   "sent_at": time.time(), "ts": time.time(), "phase": "sending"}
            self.body_confirmation = "unknown"
            self.fsm_state = None
            self._move_control_active = False
        previous_velocity_requested = self._velocity_command_active
        if cmd == 'Move':
            self._velocity_command_active = True
        options = {"api_id": spec.api_id}
        if params:
            options["parameter"] = params
        try:
            response = await asyncio.wait_for(
                connection.datachannel.pub_sub.publish_request_new(RTC_TOPIC["SPORT_MOD"], options),
                timeout=min(spec.timeout_s, 5.0))
        except asyncio.CancelledError:
            if epoch == self._motion_epoch and connection is self._conn and body_action:
                self.pending_action = {**self.pending_action, "phase": "cancelled_unconfirmed"}
            raise
        except Exception as exc:
            if epoch != self._motion_epoch or connection is not self._conn:
                cancelled = asyncio.get_running_loop().create_future()
                cancelled.set_result({"result": "cancelled", "error": "superseded_during_send"})
                return cancelled, None
            self._finish_pending("error", "disconnected")
            self.is_connected = False
            self.fsm_state = "offline"
            self.body_confirmation = "unknown"
            raise RuntimeError(f"发送失败，连接已断开: {exc}") from exc
        if epoch != self._motion_epoch or connection is not self._conn or not self.is_connected:
            cancelled = asyncio.get_running_loop().create_future()
            cancelled.set_result({"result": "cancelled", "error": "superseded_during_send"})
            return cancelled, response
        packet = response.get("data") if isinstance(response, dict) else None
        header = packet.get("header") if isinstance(packet, dict) else None
        status = header.get("status") if isinstance(header, dict) else None
        code = status.get("code") if isinstance(status, dict) else None
        if type(code) is int and code != 0:
            if cmd == 'Move':
                self._velocity_command_active = previous_velocity_requested
            if body_action:
                self.runtime.finish_body_action(request_id, 'error', phase='rejected',
                                                error='sport_rpc_rejected')
                self._action_observation = None
                self.pending_action = old_pending
                self.fsm_state = source
                self.body_confirmation = "previous_observation"
            return None, response
        if body_action:
            observation = self._action_observation
            observation['acknowledged'] = True
            self.pending_action = {**self.pending_action,
                                   "phase": "executing" if observation['motion_seen'] else
                                   "accepted" if code == 0 else "sent"}
            self._action_deadline = asyncio.get_running_loop().call_later(
                spec.timeout_s, self._expire_body_action, request_id)
            if spec.completion == "joint_posture" and self._joint_observation_available:
                waiter = asyncio.get_running_loop().create_future()
                self._completion = waiter
                self._completion_target = spec.expected_state
                self._completion_samples = 0
        elif cmd == "StopMove":
            self._velocity_command_active = False
            self._move_control_active = False
            if self.pending_action is not None:
                self.pending_action = {**self.pending_action, "phase": "stop_requested_unconfirmed"}
            self.fsm_state = next_state
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

    def move_velocity(self, vx: float, vy: float, vyaw: float, *, deadman_s: float | None = None) -> dict:
        """发送速度指令，通过 WIRELESS_CONTROLLER 模拟摇杆输入。

        fire-and-forget（不等 ACK），适合摇杆高频实时输入，不阻塞导航循环。
        坑：连接后不要自动 switch_mode("normal")——实测会让机器狗进入忽略遥控
        的模式，WIRELESS_CONTROLLER 与 SPORT_MOD Move 都会失效；默认模式即可遥控。
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
        try:
            self._conn.datachannel.pub_sub.publish_without_callback(
                RTC_TOPIC["WIRELESS_CONTROLLER"],
                data={"lx": -vy, "ly": vx, "rx": -vyaw, "ry": 0},
            )
        except Exception as exc:
            self.is_connected = False
            self.fsm_state = "offline"
            raise RuntimeError(f"发送失败，连接已断开: {exc}") from exc
        self._finish_pending("cancelled", "velocity_override")
        self._motion_epoch += 1
        self._velocity_command_active = active
        if active:
            self._motion_observation_enabled = True
            self._stationary_samples = 0
        elif self.fsm_state == "moving":
            self.runtime.body(None, confirmation="unknown")
        self._state_changed()
        if self._velocity_timer:
            self._velocity_timer.cancel()
            self._velocity_timer = None
        if deadman_s and any((vx, vy, vyaw)):
            self._velocity_timer = asyncio.get_running_loop().call_later(deadman_s, self._expire_velocity)
        return {"result": "accepted", "confirmation": "transport", "fsm_state": target}

    def stop_velocity(self) -> dict:
        """紧急停止和会话清理同时退出遥控状态。"""
        stopped_state = self.fsm_state if self.fsm_state in {"standing", "lying", "sitting"} else None
        result = self.move_velocity(0, 0, 0)
        self._move_control_active = False
        self.runtime.commit("remote_exit", control_mode="normal")
        self.fsm_state = stopped_state
        result["fsm_state"] = stopped_state
        return result

    def _expire_velocity(self):
        self._velocity_timer = None
        try:
            self.move_velocity(0, 0, 0)
        except (RuntimeError, ValueError):
            logger.warning("velocity stop could not be sent")

    async def balance_stand(self) -> None:
        return await self.send_command("BalanceStand")

    async def set_led(self, color: str = "white", duration: int = 60) -> None:
        if color not in _VALID_LED_COLORS:
            raise ValueError(
                f"Unknown color {color!r}, choose from: {', '.join(sorted(_VALID_LED_COLORS))}"
            )
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["VUI"],
            {"api_id": 1001, "parameter": {"color": color, "time": duration}},
        )


go2 = Go2Connection()
