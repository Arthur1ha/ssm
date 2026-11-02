import asyncio
import logging
import math
import time
import uuid

from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD, OBSTACLES_AVOID_API

from cloud.go2.connection.fsm import Go2FSM, CommandBlocked, _TRANSIENT_STATES
from cloud.go2.connection.sensors import Go2Sensors
from cloud.go2.connection.video import Go2Video

logger = logging.getLogger(__name__)

_VALID_LED_COLORS = frozenset({"white", "red", "yellow", "blue", "green", "cyan", "purple"})
_RPC_CONFIRMED_GESTURES = frozenset({"Hello"})
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
        self._velocity_timer = None
        self._move_control_active = False
        self._odom_session_id = uuid.uuid4().hex
        self._latest_frame = None
        self._latest_frame_meta = None

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def connect(self, email: str, password: str, serial: str, region: str = "cn") -> None:
        self._connected_serial = None
        self._latest_frame = None
        self._latest_frame_meta = None
        self._move_control_active = False
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

        try:
            await self._conn.connect()
        except Exception as exc:
            self.is_connected = False
            self.fsm_state = "offline"
            logger.error("[Go2/Conn] 连接失败: %s", exc)
            raise

        # Native 解码器返回米制点云（ndarray N×3），比 LibVoxel 的格索引更易过滤地板
        self._conn.datachannel.set_decoder(decoder_type="native")

        self._conn.datachannel.pub_sub.subscribe(RTC_TOPIC["LF_SPORT_MOD_STATE"],          self._on_state)
        self._conn.datachannel.pub_sub.subscribe(RTC_TOPIC["ROBOTODOM"],                    self._on_odom)
        self._conn.datachannel.pub_sub.subscribe(RTC_TOPIC["LOW_STATE"],                    self._on_low_state)
        self._conn.datachannel.pub_sub.subscribe(RTC_TOPIC["ULIDAR_ARRAY"],                 self._on_voxel_map)
        self._conn.datachannel.pub_sub.subscribe(RTC_TOPIC["LIDAR_NAVIGATION_GLOBAL_PATH"], self._on_global_path)
        await self._conn.datachannel.disableTrafficSaving(True)

        self.is_connected = True
        self.fsm_state = "standing"
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

        # 激活 Sport 模块：连接后不发 SPORT_MOD 命令时 WIRELESS_CONTROLLER 会被静默忽略
        try:
            await self.balance_stand()
        except Exception:
            pass

    async def disconnect(self) -> None:
        self._connected_serial = None
        self._latest_frame = None
        self._latest_frame_meta = None
        self._move_control_active = False
        if self._velocity_timer:
            self._velocity_timer.cancel()
        self._finish_pending("error", "disconnected")
        self.is_connected = False
        self.fsm_state = "offline"
        self._last_state_received_at = None
        self._sport_state_observed_at = None
        self.reset_sensor_epoch()
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
        if self._completion is not None and not self._completion.done():
            self._completion.set_result({"result": result, "error": error})
        self._completion = None
        self._completion_target = None
        self._completion_samples = 0

    def _validate_motion(self, cmd: str, params: dict | None = None) -> str:
        target = self.fsm_next(cmd)
        # Continuous movement updates and emergency stop are idempotent.
        if cmd == "Move" and self.fsm_state == "moving":
            target = "moving"
        if cmd == "StopMove" and self.fsm_state not in ("offline", "connecting"):
            target = "lying" if self.fsm_state == "lying" else "standing"
        if cmd == "BalanceStand" and self.fsm_state == "standing":
            target = "standing"
        if target is None:
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
            if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
                raise ValueError("velocity outside supported range (0.5 m/s, 0.8 rad/s)")

    async def send_command(self, cmd: str, params: dict | None = None, *, completion_timeout: float = 20.0) -> dict:
        async with self._command_lock:
            waiter, response = await self._send_command(cmd, params)
        header = ((response or {}).get("data") or {}).get("header") or {}
        status = header.get("status") or {}
        request_status_code = status.get("code")
        if cmd in _RPC_CONFIRMED_GESTURES and request_status_code is not None:
            # Unitree's progress field reports dance activity, not Hello.
            # Its matching Sport RPC response is the device result.
            if self._completion is waiter:
                self._completion = None
                self._completion_target = None
                self._completion_samples = 0
            if waiter is not None and not waiter.done():
                waiter.cancel()
            if self.fsm_state in _TRANSIENT_STATES:
                self.fsm_state = "standing"
            if request_status_code != 0:
                return {"result": "error", "error": "sport_rpc_rejected",
                        "request_status_code": request_status_code, "fsm_state": self.fsm_state}
            return {"result": "ok", "confirmation": "sport_rpc",
                    "request_status_code": 0, "fsm_state": self.fsm_state}
        if waiter is None:
            return {"result": "accepted", "confirmation": "transport", "fsm_state": self.fsm_state,
                    "request_status_code": request_status_code}
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=completion_timeout)
        except asyncio.TimeoutError:
            # Some firmware actions do not publish a complete progress cycle.
            # Stop the action before leaving its transient state; do not claim completion.
            age = (None if self._last_state_received_at is None else
                   round(time.monotonic() - self._last_state_received_at, 2))
            logger.warning("[Go2/Conn] 动作完成未确认 cmd=%s fsm=%s progress=%.3f telemetry_age_s=%s",
                           cmd, self.fsm_state, self._last_progress, age)
            if self._completion is waiter and self.fsm_state in _TRANSIENT_STATES and self.is_connected:
                async with self._command_lock:
                    if self._completion is waiter and self.fsm_state in _TRANSIENT_STATES:
                        try:
                            await self._send_command("StopMove", None)
                        except Exception as exc:
                            logger.warning("[Go2/Conn] 动作超时后停止失败: %s", exc)
            return {"result": "timeout", "error": "completion_not_observed", "fsm_state": self.fsm_state,
                    "request_status_code": request_status_code}
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
        if cmd == "EnterMoveControl":
            target = self._validate_motion(cmd, params)
            self._move_control_active = True
            self.fsm_state = target
            return None, None
        api_id = SPORT_CMD.get(cmd)
        if api_id is None:
            raise ValueError(f"Unknown command: {cmd}")
        next_state = self._validate_motion(cmd, params)
        epoch = self._motion_epoch
        options: dict = {"api_id": api_id}
        if params:
            options["parameter"] = params
        # A gesture can finish before the request response arrives. Arm its
        # progress waiter before publishing so the state callback sees the
        # entire motion, including a fast rise/fall during that wait.
        early_waiter = None
        if next_state in _TRANSIENT_STATES:
            self._finish_pending("cancelled", "superseded")
            self._last_progress = 0.0
            self._move_control_active = False
            self.fsm_state = next_state
            early_waiter = asyncio.get_running_loop().create_future()
            self._completion = early_waiter
            self._completion_target = None
            self._completion_samples = 0
            self._state_probe_count = 0
        try:
            response = await asyncio.wait_for(
                self._conn.datachannel.pub_sub.publish_request_new(RTC_TOPIC["SPORT_MOD"], options), timeout=5.0)
        except Exception as exc:
            self.is_connected = False
            self.fsm_state = "offline"
            self._finish_pending("error", "disconnected")
            raise RuntimeError(f"发送失败，连接已断开: {exc}") from exc
        logger.info("[Go2/Conn] 发送命令: %s params=%s", cmd, params)
        if next_state in _TRANSIENT_STATES:
            logger.info("[Go2/Conn] 动作请求响应 cmd=%s response=%s", cmd, str(response)[:700])

        if epoch != self._motion_epoch or not self.is_connected:
            waiter = asyncio.get_running_loop().create_future()
            waiter.set_result({"result": "cancelled", "error": "superseded_during_send"})
            return waiter, response

        if early_waiter is not None:
            return early_waiter, response

        self._finish_pending("cancelled", "superseded")
        self._last_progress = 0.0
        self._move_control_active = False
        self.fsm_state = next_state
        if next_state in _TRANSIENT_STATES or cmd in {"StandUp", "StandDown"}:
            self._completion = asyncio.get_running_loop().create_future()
            self._completion_target = next_state if cmd in {"StandUp", "StandDown"} else None
            self._completion_samples = 0
            return self._completion
        return self._completion, response

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
            "Move" if active else "StopMove", {"x": vx, "y": vy, "z": vyaw},
        )
        if not active and self._move_control_active and self.fsm_state == "moving":
            target = "moving"  # 摇杆归零只停运动；由 StopMove 退出控制状态。
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
        self.fsm_state = target
        if self._velocity_timer:
            self._velocity_timer.cancel()
            self._velocity_timer = None
        if deadman_s and any((vx, vy, vyaw)):
            self._velocity_timer = asyncio.get_running_loop().call_later(deadman_s, self._expire_velocity)
        return {"result": "accepted", "confirmation": "transport", "fsm_state": target}

    def stop_velocity(self) -> dict:
        """紧急停止和会话清理同时退出遥控状态。"""
        stopped_state = "lying" if self.fsm_state == "lying" else "standing"
        result = self.move_velocity(0, 0, 0)
        self._move_control_active = False
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
