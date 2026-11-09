"""Lamp sensor listener and live autonomy gates for the LangGraph workflow."""

import json
import logging
import queue
import threading
import time

from cloud.esp32.perception import sense as sense_sensors

logger = logging.getLogger(__name__)


async def run_training_behavior(prepared, context, *, run_id, agent, dispatch=None,
                                availability=None):
    """Run lamp teachings through the shared ordered Graph and MQTT boundary."""
    from dataclasses import asdict
    from cloud.agents.common.training.workflow import run_training_workflow
    from cloud.cards.registry import get_registry
    from cloud.esp32.tool_executor import execute_physical
    from cloud.esp32.tool_registry import PHYSICAL_ACTIONS
    from cloud.esp32.physical_tasks import stable_task_id

    unit_id = prepared['unit_id']
    active = getattr(agent, '_training_behavior_run_id', None)
    if active is not None and active != run_id:
        return {'result': 'blocked', 'error': 'training_behavior_busy'}
    agent._training_behavior_run_id = run_id
    command_epoch = getattr(agent, '_command_epoch', 0)
    autonomy_epoch = getattr(agent, '_autonomy_epoch', 0)

    def available():
        if getattr(agent, '_command_epoch', 0) != command_epoch:
            return False
        if context.origin_kind == 'agent_autonomous' and (
                agent.get_autonomy_mode() != 'reactive' or agent._in_user_hold()
                or getattr(agent, '_autonomy_epoch', 0) != autonomy_epoch):
            return False
        return availability is None or availability()

    def snapshot():
        card = get_registry().get_card(unit_id) or {}
        return {**(agent._state.actuator_snapshot().get(unit_id, {}).get('state') or {}),
                'connected': card.get('online') is True}

    def before_step(resolved, trusted):
        if not available():
            return {'result': 'cancelled', 'error': 'lamp_control_changed'}
        return None

    def send(uid, action, params, trusted, source, *, task_id):
        if dispatch is not None:
            return dispatch(uid, action, params, trusted, source, task_id=task_id)
        from cloud.api.intents import _dispatch_mqtt_task
        return _dispatch_mqtt_task(uid, action, params, asdict(trusted),
                                  source=source, task_id=task_id)

    def execute(action, params, trusted, request_id):
        if action not in PHYSICAL_ACTIONS:
            return {'result': 'rejected', 'error': 'training_action_unavailable'}
        card = get_registry().get_card(unit_id)
        return execute_physical(
            state=agent._state, unit_id=unit_id, action=action, params=params,
            context=trusted, dispatch=send, source='lamp_training', card=card,
            task_id=stable_task_id(unit_id, run_id, request_id),
            preflight=lambda current, arguments: None if available() else 'lamp_control_changed')

    def observe(receipt):
        from cloud.esp32.physical_tasks import get
        return get(stable_task_id(unit_id, run_id, receipt['request_id']),
                   unit_id=unit_id, session_id=context.session_id)

    try:
        return await run_training_workflow(prepared, context, run_id=run_id,
                                           snapshot=snapshot, execute=execute,
                                           before_step=before_step, observe=observe)
    finally:
        if getattr(agent, '_training_behavior_run_id', None) == run_id:
            agent._training_behavior_run_id = None


class ReactiveMind:
    def _in_user_hold(self) -> bool:
        """当前是否处于用户让位窗口内。"""
        return time.time() < self._user_hold_until

    def _should_act(self) -> bool:
        """自主层本拍是否应主动行动：manual 或处于用户让位窗口时返回 False。"""
        if self._autonomy_mode == "manual" or getattr(self, '_training_behavior_run_id', None):
            return False
        if self._in_user_hold():
            return False
        return True

    def _device_online(self) -> bool:
        from cloud.cards.registry import get_registry
        card = get_registry().get_card(self._find_led_unit())
        return bool(card and card.get("online"))

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True, name="ESP32Agent")
        t.start()

    def push_sensor_event(self, unit_id: str, payload: dict):
        now = time.time()
        if unit_id.endswith("_sound"):
            self._last_sound_ts = now
        self._event_queue.put({"unit_id": unit_id, "payload": payload, "enqueue_ts": now,
                               "autonomy_epoch": self._autonomy_epoch,
                               "eligible_at_arrival": self._should_act()})

    def _sense(self) -> dict | None:
        observed = sense_sensors(self._state, self._last_sound_ts)
        return observed if observed["light_available"] or observed["sound_available"] else None

    def _find_led_unit(self) -> str:
        """从 actuator 快照中查找 LED 单元 ID，找不到返回默认值。"""
        for uid in self._state.actuator_snapshot():
            if uid.endswith("_led"):
                return uid
        return "esp32_desk_led"

    def _get_current_ism(self, led_unit: str) -> str:
        """读取 LED 当前 ISM 状态。"""
        snap = self._state.actuator_snapshot()
        return (snap.get(led_unit, {}).get("state") or {}).get("ism", "").upper()

    def _dispatch_tool(self, tool_name: str, params: dict, led_unit: str,
                       *, task_id: str | None = None) -> bool:
        """单一执行口：冷却判定 → 执行 → 记录。返回是否真正执行。

        set_led_state / set_led_color / blink_led 共享 self._cooldown（同参 5 分钟内只发一次）；
        色温/亮度由上游 LLM 决定，此处不做任何颜色逻辑。
        """
        from cloud.esp32.tool_registry import (
            TOOL_DECLARATIONS, valid_auxiliary_params, valid_lamp_params,
        )
        self._last_reactive_tool_result = {"result": "blocked", "error": "reactive_preflight"}
        now = time.time()
        if not isinstance(params, dict) or tool_name not in TOOL_DECLARATIONS:
            return False
        params = dict(params)

        if tool_name == "send_message":
            if not valid_auxiliary_params("send_message", params):
                self._last_reactive_tool_result = {"result": "rejected", "error": "invalid_message_params"}
                return False
            from cloud.peers.social import activity_peers
            from cloud.space.context import get_space_context
            context = get_space_context(led_unit, state=self._state)
            activity, peers = activity_peers(led_unit, context["peers"])
            target = str(params.get("target") or "")
            topic = str(params.get("content") or "")
            if activity and self._social_state.activity_id != activity["activity_id"]:
                self._social_state.reset(activity["activity_id"])
            if (not activity or target not in {peer["unit_id"] for peer in peers}
                    or not self._social_state.can_send(target, topic)):
                self._last_reactive_tool_result = {"result": "blocked", "error": "social_gate"}
                return False

        action = TOOL_DECLARATIONS[tool_name].get("action")
        if action:
            from cloud.cards.registry import get_registry
            card = get_registry().get_card(led_unit)
            valid = valid_lamp_params(action, params, card=card)
            if not valid and action == 'BLINK' and 'count' not in params:
                from cloud.esp32.tool_registry import BLINK_DEFAULT
                valid = valid_lamp_params(action, {**params, 'count': BLINK_DEFAULT}, card=card)
            if not valid:
                logger.warning("invalid reactive tool arguments: %s %s", tool_name, params)
                return False

        if tool_name in ("set_led_state", "set_led_color", "blink_led"):
            params["unit_id"] = led_unit
            key = f"{tool_name}_{json.dumps(params, sort_keys=True)}"
            if now - self._cooldown.get(key, 0) < 300:
                logger.debug("cooldown skip: %s", key)
                self._last_reactive_tool_result = {"result": "blocked", "error": "repeat_action_cooldown"}
                return False

        try:
            if tool_name in {"list_peers", "get_space_context", "request_peer_skill", "send_message"}:
                from cloud.esp32.tool_executor import execute_auxiliary
                from cloud.space.execution import ExecutionContext
                result = execute_auxiliary(
                    unit_id=led_unit, tool=tool_name, params=params,
                    context=ExecutionContext("lamp-autonomous", "agent", False,
                                             "main", "agent_autonomous", 0),
                    mode_available=self._should_act(),
                )
                self._last_reactive_tool_result = result
                if result.get("result") not in {"ok", "accepted"}:
                    logger.warning("auxiliary tool did not complete: %s", result)
                    return False
                if tool_name == "send_message":
                    self._social_state.sent(target, topic)
                if tool_name not in {"list_peers", "get_space_context"}:
                    self._last_act_ts = time.time()
                return True
            if tool_name in ("set_led_state", "set_led_color", "blink_led"):
                from cloud.cards.registry import get_registry
                from cloud.esp32.tool_executor import execute_physical
                from cloud.space.execution import ExecutionContext
                card = get_registry().get_card(led_unit)
                def preflight(current, arguments):
                    if not self._should_act():
                        return "autonomy_unavailable"
                    if not card:
                        return "lamp_unavailable"
                    return None
                def dispatch(_uid, _action, arguments, _ctx, _src, *, task_id=None):
                    from cloud.api.intents import _dispatch_mqtt_task
                    return _dispatch_mqtt_task(
                        _uid, _action, arguments,
                        {"session_id": "agent_auto", "actor": "agent", "persist": False},
                        source="lamp_reactive", task_id=task_id,
                    )
                result = execute_physical(
                    state=self._state, unit_id=led_unit, action=action,
                    params={k: v for k, v in params.items() if k != "unit_id"},
                    context=ExecutionContext("agent_auto", "agent", False,
                                             "main", "agent_autonomous", 0),
                    dispatch=dispatch, task_id=task_id,
                    source="lamp_reactive", preflight=preflight,
                    card=card,
                )
                self._last_reactive_tool_result = result
                if result.get("result") != "ok":
                    logger.warning("reactive command did not complete: %s", result)
                    return False
                self._cooldown[key] = time.time()
                self._last_act_ts = time.time()
            else:
                return False
            logger.info("execute %s %s", tool_name, params)
            return True
        except Exception as e:
            logger.warning("tool %s failed: %s", tool_name, e)
            self._last_reactive_tool_result = {"result": "error", "error": type(e).__name__}
            return False

    def _check_proactive(self, sense: dict) -> list[str]:
        now = time.time()
        current_level = sense.get("settled_light_level", sense.get("light_level", ""))
        level_changed = bool(self._last_light_level not in ("", "UNKNOWN")
                             and current_level not in ("", "UNKNOWN")
                             and current_level != self._last_light_level)
        self._last_light_level = current_level
        if now - self._last_proactive_ts < 600:
            return []
        triggers = []

        is_active = sense.get("sound_detected", False)
        if is_active:
            if self._work_start_ts == 0:
                self._work_start_ts = now
        else:
            self._work_start_ts = 0

        if self._work_start_ts > 0 and (now - self._work_start_ts) >= 3600:
            triggers.append("long_work")

        if level_changed:
            triggers.append("env_changed")

        if self._last_act_ts > 0 and (now - self._last_act_ts) >= 300:
            triggers.append("unimproved")

        if triggers:
            self._last_proactive_ts = now
        return triggers

    def _loop(self):
        logger.info("sensor worker started (autonomy mode=%s)", self._autonomy_mode)
        while True:
            try:
                try:
                    event = self._event_queue.get(timeout=30)
                except queue.Empty:
                    event = {}
                from cloud.agents.lamp.workflows.reactive import run_reactive_sensor_event
                result = run_reactive_sensor_event(self, event)
                if result is not None:
                    logger.info("reactive graph finished status=%s run=%s",
                                result.get("status"), result.get("run_id"))
            except Exception:
                logger.exception("sensor listener error")
