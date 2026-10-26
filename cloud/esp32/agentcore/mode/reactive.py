"""Lamp reactive mode: sensor loop, cooldown, user hold and result-aware actions."""

import json
import logging
import queue
import re
import threading
import time

from langchain_core.messages import HumanMessage

from cloud.esp32 import tools as _tools
from cloud.esp32.agentcore.soul import get_personality
from cloud.esp32.agentcore.perception.sensors import sense as sense_sensors

logger = logging.getLogger(__name__)


class ReactiveMind:
    def _in_user_hold(self) -> bool:
        """当前是否处于用户让位窗口内。"""
        return time.time() < self._user_hold_until

    def _should_act(self) -> bool:
        """自主层本拍是否应主动行动：manual 或处于用户让位窗口时返回 False。"""
        if self._autonomy_mode == "manual":
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
        self._event_queue.put({"unit_id": unit_id, "payload": payload, "enqueue_ts": now})

    def _sense(self) -> dict | None:
        observed = sense_sensors(self._state, self._last_sound_ts)
        return observed if observed["light_available"] else None

    def _reason(self, sense_data: dict) -> list[dict] | None:
        from cloud.esp32 import tools as _tools_mod

        if self._belief_summary:
            history_lines = f"历史规律摘要：{self._belief_summary}"
        elif self._belief_history:
            recent = self._belief_history[-3:]
            lines = []
            for b in recent:
                ts_str = time.strftime("%H:%M", time.localtime(b.get("ts", 0)))
                actions = "、".join(b.get("actions", [])) or "无动作"
                lines.append(f"- {ts_str}: {b.get('context', '')}（执行了：{actions}）")
            history_lines = "近期历史：\n" + "\n".join(lines)
        else:
            history_lines = ""

        proactive_hints = sense_data.get("proactive_hints", [])
        hint_labels = {
            "long_work":   "用户已连续活动超过 60 分钟",
            "unimproved":  "距上次执行动作超过 5 分钟",
            "env_changed": "环境光线档位发生变化",
        }
        hints_str = ""
        if proactive_hints:
            hint_strs = "、".join(hint_labels.get(h, h) for h in proactive_hints)
            hints_str = f"\n提示信息（供参考，不强制触发动作）：{hint_strs}"

        from cloud.esp32.agentcore.memory import taught as _taught
        taught_rules = [r for r in _taught.list_all() if r.get("enabled", True)]
        if taught_rules:
            taught_lines = "\n".join(
                f'- [{r["id"]}] 当「{r["trigger"]}」时：{r["behavior"]}'
                for r in taught_rules
            )
            taught_str = (
                "\n\n主人教过的规矩（命中时，在对应工具调用里加 "
                '"taught_id" 字段标明依据的规矩 id）：\n' + taught_lines
            )
        else:
            taught_str = ""

        try:
            peer_view = _tools_mod.list_peers(self._find_led_unit())
        except Exception:
            peer_view = []
        prompt = (
            f"{get_personality()}\n"
            "你负责控制桌面 LED 灯，根据当前传感器观测自主决定是否采取行动。\n\n"
            f"当前观测：{json.dumps(sense_data, ensure_ascii=False)}\n"
            "字段说明：light_level（DARK/DIM/NORMAL/BRIGHT）、light_lux（照度，越小越暗）、"
            "sound_detected（近 5 秒内是否有声音）、led_state（当前灯状态）、"
            "led_config（当前 RGB、亮度、闪烁次数）、time_period（时段）\n\n"
            f"{_tools_mod.TOOL_DESCRIPTIONS}\n\n"
            f"当前同伴：{json.dumps(peer_view, ensure_ascii=False)}\n\n"
            f"{history_lines}"
            f"{hints_str}{taught_str}\n\n"
            "根据观测自主判断，输出 JSON 数组，无需任何动作时输出 []，不含代码块或解释。\n"
            "示例：[{\"tool\": \"set_led_color\", \"params\": {\"r\": 255, \"g\": 160, \"b\": 60, \"brightness\": 160}}, "
            "{\"tool\": \"speak\", \"params\": {\"text\": \"傍晚了，给你调个暖黄\"}}]"
        )

        try:
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            content = resp.content.strip()
            content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("`").strip()
            idx_s, idx_e = content.find("["), content.rfind("]")
            if idx_s == -1:
                raise ValueError("no JSON array found")
            tool_calls = json.loads(content[idx_s:idx_e + 1])
            if not isinstance(tool_calls, list):
                raise ValueError("expected list")
            return [c for c in tool_calls if isinstance(c, dict) and "tool" in c]
        except Exception as e:
            logger.warning("reason parse error: %s", e)
            return None

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

    def _dispatch_tool(self, tool_name: str, params: dict, led_unit: str) -> bool:
        """单一执行口：冷却判定 → 执行 → 记录。返回是否真正执行。

        set_led_state / set_led_color / blink_led 共享 self._cooldown（同参 5 分钟内只发一次）；
        speak 等不受冷却约束。色温/亮度由上游 LLM 决定，此处不做任何颜色逻辑。
        """
        from cloud.esp32 import tools as _tools_mod
        from cloud.esp32.agentcore.tools.registry import TOOL_DECLARATIONS, valid_lamp_params
        now = time.time()
        if not isinstance(params, dict) or tool_name not in TOOL_DECLARATIONS:
            return False
        params = dict(params)

        action = TOOL_DECLARATIONS[tool_name].get("action")
        if action and not valid_lamp_params(action, params):
            logger.warning("invalid reactive tool arguments: %s %s", tool_name, params)
            return False

        if tool_name in ("set_led_state", "set_led_color", "blink_led"):
            params["unit_id"] = led_unit
            key = f"{tool_name}_{json.dumps(params, sort_keys=True)}"
            if now - self._cooldown.get(key, 0) < 300:
                logger.debug("cooldown skip: %s", key)
                return False

        if tool_name not in _tools_mod.TOOL_FN_MAP:
            logger.warning("unknown tool: %s", tool_name)
            return False
        fn = getattr(_tools_mod, tool_name, None)
        if fn is None:
            logger.warning("unknown tool: %s", tool_name)
            return False
        try:
            if tool_name in {"list_peers", "request_peer_skill", "speak"}:
                from cloud.esp32.agentcore.tools.executor import execute_auxiliary
                from cloud.space.execution import ExecutionContext
                result = execute_auxiliary(
                    unit_id=led_unit, tool=tool_name, params=params,
                    context=ExecutionContext("lamp-autonomous", "agent", False,
                                             "main", "agent_autonomous", 0),
                    mode_available=self._should_act(),
                )
                if result.get("result") not in {"ok", "accepted"}:
                    logger.warning("auxiliary tool did not complete: %s", result)
                    return False
                if tool_name != "list_peers":
                    self._last_act_ts = time.time()
                return True
            if tool_name in ("set_led_state", "set_led_color", "blink_led"):
                from cloud.cards.registry import get_registry
                from cloud.esp32.agentcore.tools.executor import execute_physical
                from cloud.esp32.agentcore.tools.registry import fsm_allows
                from cloud.space.execution import ExecutionContext
                def preflight(current, arguments):
                    if not self._should_act():
                        return "autonomy_unavailable"
                    card = get_registry().get_card(led_unit)
                    if not card or not card.get("online"):
                        return "target_offline"
                    if not fsm_allows(card, str(current.get("ism") or "").upper(), action, arguments):
                        return "invalid_fsm_transition"
                    return None
                result = execute_physical(
                    state=self._state, unit_id=led_unit, action=action,
                    params={k: v for k, v in params.items() if k != "unit_id"},
                    context=ExecutionContext("lamp-autonomous", "agent", False,
                                             "main", "agent_autonomous", 0),
                    dispatch=lambda _uid, _action, arguments, _ctx, _src:
                        fn(unit_id=_uid, **arguments),
                    source="lamp_reactive", preflight=preflight,
                )
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
            return False

    def _execute(self, tool_calls: list, led_unit: str):
        """执行 planner 输出的工具调用列表（逐个走单一执行口）。

        若某调用带 taught_id 且执行成功，记一次调教命中。
        """
        from cloud.esp32.agentcore.memory import taught as _taught
        completed = []
        for call in tool_calls[:4]:
            if not isinstance(call, dict):
                break
            ok = self._dispatch_tool(call.get("tool", ""), call.get("params", {}), led_unit)
            if not ok:
                break  # do not narrate success or run dependent actions after a failed command
            completed.append(call.get("tool", ""))
            if ok and call.get("taught_id"):
                _taught.touch(call["taught_id"])
        return completed

    def _check_proactive(self, sense: dict) -> list[str]:
        now = time.time()
        current_level = sense.get("light_level", "")
        level_changed = bool(self._last_light_level and current_level != self._last_light_level)
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

    def _summarize_beliefs(self) -> str:
        recent = self._belief_history[-5:]
        entries = []
        for b in recent:
            ts_str = time.strftime("%H:%M", time.localtime(b.get("ts", 0)))
            actions = "、".join(b.get("actions", [])) or "无动作"
            entries.append(f"{ts_str}: {b.get('context', '')} → {actions}")
        prompt = "以下是最近桌面空间状态记录，用一句话总结规律（20字以内）：\n" + "\n".join(entries)
        try:
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            return resp.content.strip()[:100]
        except Exception as e:
            logger.warning("summarize error: %s", e)
            return ""

    def _loop(self):
        logger.info("sensor worker started (autonomy mode=%s)", self._autonomy_mode)
        _last_event_ts: dict[str, float] = {}
        DEBOUNCE_SECS     = 5
        OFFLINE_THRESHOLD = 120
        _last_any_event_ts: float = 0.0

        while True:
            try:
                triggered_by_event = False
                try:
                    event = self._event_queue.get(timeout=30)
                    unit_id    = event.get("unit_id", "")
                    enqueue_ts = event.get("enqueue_ts", time.time())
                    _last_any_event_ts = enqueue_ts
                    if enqueue_ts - _last_event_ts.get(unit_id, 0) < DEBOUNCE_SECS:
                        continue
                    _last_event_ts[unit_id] = enqueue_ts
                    triggered_by_event = True
                    logger.debug("sensor event received from %s", unit_id)
                except queue.Empty:
                    if time.time() - _last_any_event_ts > OFFLINE_THRESHOLD:
                        logger.debug("periodic tick: device offline, skip")
                        continue
                    logger.debug("periodic tick")

                if not self._should_act() or not self._device_online():
                    logger.debug("autonomy gated (mode=%s hold=%s), skip self-action",
                                 self._autonomy_mode, self._in_user_hold())
                    continue

                sense = self._sense()
                if sense is None:
                    logger.debug("sense: no light data, skip")
                    continue
                logger.debug("sense: %s", sense)

                sense["proactive_hints"] = self._check_proactive(sense)

                # 仅在感知、灯配置或调教规则变化时重新评估；事件去抖和最小间隔限制 LLM 频率。
                from cloud.esp32.agentcore.memory import taught as _taught
                rule_sig = tuple((r.get("id"), r.get("trigger"), r.get("behavior"), r.get("enabled"))
                                 for r in _taught.list_all())
                led_config = sense.get("led_config") or {}
                try:
                    peers = _tools.list_peers(self._find_led_unit())
                except Exception:
                    peers = []
                peer_sig = tuple((peer.get("unit_id"), peer.get("online"),
                                  (peer.get("state_summary") or {}).get("fsm_state"))
                                 for peer in peers)
                sig = (sense["light_level"], sense["time_period"],
                       sense["sound_detected"], sense["led_state"],
                       tuple(led_config.get(k) for k in ("r", "g", "b", "brightness", "count")),
                       rule_sig, peer_sig)
                if sig == self._last_reason_sig:
                    logger.debug("no material change %s, skip LLM", sig)
                    continue
                if time.time() - self._last_reason_ts < 10:
                    logger.debug("reason rate limit, wait for next tick")
                    continue

                self._last_reason_ts = time.time()
                tool_calls = self._reason(sense)
                if tool_calls is None:
                    continue
                if not self._should_act() or not self._device_online():
                    # A user, mode change or offline status may have arrived
                    # while model inference was in progress. Keep the context
                    # eligible for reassessment after the gate opens.
                    continue
                self._last_reason_sig = sig   # 这个情境已评估过，下次相同则跳过

                logger.info("tool_calls (%d): %s", len(tool_calls), json.dumps(tool_calls, ensure_ascii=False))

                # Mode or user hold can change while the LLM is reasoning.
                completed = (self._execute(tool_calls, self._find_led_unit())
                             if tool_calls and self._should_act() else [])
                self._belief_history.append({
                    "ts": time.time(),
                    "context": f"light={sense['light_level']} sound={sense['sound_detected']} led={sense['led_state']}",
                    "actions": completed,
                })
                if len(self._belief_history) > 10:
                    self._belief_history.pop(0)

                self._beliefs_since_summary += 1
                if self._beliefs_since_summary >= 5:
                    self._belief_summary = self._summarize_beliefs()
                    self._beliefs_since_summary = 0

            except Exception as e:
                logger.error("loop error: %s", e, exc_info=True)
