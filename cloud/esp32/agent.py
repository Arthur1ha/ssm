import os
import re
import json
import queue
import time
import datetime
import logging
import threading
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

from cloud.esp32.state import ESP32State
from cloud.esp32 import tools as _tools

AGENT_PERSONA = """
你是一个桌面空间智能体，性格设定如下：
- 说话贱贱的，嘴比较刁，但不失礼貌
- 懂网络梗，偶尔用但不过度
- 对自己的判断有点自信，偶尔会吐槽环境或用户行为
- 执行动作时会带上一句评论，比如"行吧，给你开了"、"这光线也太暗了吧"
- 主动报告时语气轻松，不像机器人
- 说话简洁，不超过两句话
"""

_agent: Optional["ESP32Agent"] = None


def init(state: ESP32State, llm=None) -> "ESP32Agent":
    global _agent
    _agent = ESP32Agent(state, llm=llm)
    return _agent


def get_agent() -> Optional["ESP32Agent"]:
    return _agent


class ESP32Agent:
    def __init__(self, state: ESP32State, llm=None):
        self._state = state
        self._llm   = llm if llm is not None else self._make_llm()
        self._event_queue: queue.Queue = queue.Queue()
        self._belief_history: list[dict] = []
        self._cooldown: dict[str, float] = {}
        self._last_sound_ts: float = 0.0
        self._work_start_ts: float = 0.0
        self._last_act_ts: float = 0.0
        self._last_light_level: str = ""
        self._last_proactive_ts: float = 0.0
        self._belief_summary: str = ""
        self._beliefs_since_summary: int = 0

    def _make_llm(self):
        model_list_str = os.getenv("MODEL_LIST", os.getenv("MODEL", ""))
        models = [m.strip() for m in model_list_str.split(",") if m.strip()]
        if not models:
            models = ["doubao-seed-2-0-lite-260215"]
        base_kwargs = dict(
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0,
            timeout=30,
        )
        llms = [ChatOpenAI(model=m, **base_kwargs) for m in models]
        return llms[0].with_fallbacks(llms[1:]) if len(llms) > 1 else llms[0]

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True, name="ESP32Agent")
        t.start()

    async def run_intent(self, session_id: str, goal: str, device_ids: list) -> dict:
        device_context = []
        for device_id in device_ids:
            manifest = self._state.get_manifest(device_id)
            if manifest:
                caps = manifest.get("capabilities", [])
                device_context.append(
                    f"{device_id}（能力：{json.dumps(caps, ensure_ascii=False)}）"
                )

        if not device_context:
            registry = self._state.get_capability_registry()
            seen = set()
            for uids in registry.values():
                for uid in uids:
                    if uid not in seen:
                        seen.add(uid)
                        m = self._state.get_manifest(uid)
                        if m:
                            device_context.append(
                                f"{uid}（能力：{json.dumps(m.get('capabilities', []), ensure_ascii=False)}）"
                            )

        device_str = "\n".join(device_context) if device_context else "无可用设备"

        prompt = (
            f"你是 ESP32 设备控制智能体。根据目标生成 MQTT 控制指令列表。\n\n"
            f"目标：{goal}\n"
            f"可用设备：\n{device_str}\n\n"
            f"直接输出 JSON 数组，不含代码块或解释，每项包含 device_id、action、params。\n"
            f"action 可选：SET_STATE（params: {{state: BRIGHT|DIM|OFF}}）、"
            f"SET_COLOR（params: {{r,g,b,brightness 0-255}}）、PLAY（params: {{pattern: NOTIFY|ALERT}}）\n"
            f"示例：[{{\"device_id\": \"esp32_desk_led\", \"action\": \"SET_COLOR\", "
            f"\"params\": {{\"r\": 255, \"g\": 200, \"b\": 100, \"brightness\": 180}}}}]"
        )

        try:
            resp = await self._llm.ainvoke([HumanMessage(content=prompt)])
            content = resp.content.strip()
            content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("`").strip()
            idx_s, idx_e = content.find("["), content.rfind("]")
            cmds = json.loads(content[idx_s:idx_e + 1]) if idx_s != -1 else []
        except Exception as e:
            logger.warning("run_intent parse error: %s", e)
            cmds = []

        task_ids = []
        for i, cmd in enumerate(cmds):
            if not isinstance(cmd, dict) or "device_id" not in cmd or "action" not in cmd:
                continue
            task_id = f"{session_id}_t{i}"
            _tools.publish_task(cmd["device_id"], task_id, cmd["action"], cmd.get("params", {}), session_id)
            task_ids.append(task_id)
            logger.info("intent → %s %s task_id=%s", cmd["device_id"], cmd["action"], task_id)

        return {"task_ids": task_ids, "status": "dispatched"}

    def push_sensor_event(self, unit_id: str, payload: dict):
        now = time.time()
        if unit_id.endswith("_sound"):
            self._last_sound_ts = now
        self._event_queue.put({"unit_id": unit_id, "payload": payload, "enqueue_ts": now})

    def _sense(self) -> dict | None:
        snap = self._state.sensor_snapshot()

        light_data = None
        for unit_id, data in snap.items():
            if unit_id.endswith("_light"):
                light_data = data.get("state") or data.get("event")
                break
        if not light_data:
            return None

        level = light_data.get("level", "NORMAL")
        value = light_data.get("value", 0)
        lux   = light_data.get("lux", value)
        now   = time.time()

        server_sound_recent = (now - self._last_sound_ts) < 5
        snap_sound_recent = False
        for uid, data in snap.items():
            if uid.endswith("_sound"):
                evt_ts = (data.get("event") or {}).get("ts", 0)
                if evt_ts and (now - evt_ts) < 5:
                    snap_sound_recent = True
                break

        sound_detected = server_sound_recent or snap_sound_recent

        led_state = "UNKNOWN"
        led_device_id = "esp32_desk_led"
        for uid, data in self._state.actuator_snapshot().items():
            if uid.endswith("_led"):
                led_state = (data.get("state") or {}).get("ism", "UNKNOWN")
                led_device_id = uid
                break

        now_dt = datetime.datetime.now()
        time_str = now_dt.strftime("%H:%M")
        hour = now_dt.hour
        if hour < 6:    time_period = "深夜"
        elif hour < 9:  time_period = "清晨"
        elif hour < 12: time_period = "上午"
        elif hour < 18: time_period = "下午"
        elif hour < 21: time_period = "傍晚"
        else:           time_period = "夜间"

        return {
            "light_level": level, "light_value": value, "light_lux": lux,
            "sound_detected": sound_detected, "sound_recent": sound_detected,
            "led_state": led_state, "led_device_id": led_device_id,
            "time_str": time_str, "time_period": time_period,
        }

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

        prompt = (
            f"{AGENT_PERSONA}\n"
            "你负责控制桌面 LED 灯，根据当前传感器观测自主决定是否采取行动。\n\n"
            f"当前观测：{json.dumps(sense_data, ensure_ascii=False)}\n"
            "字段说明：light_level（DARK/DIM/NORMAL/BRIGHT）、light_lux（照度，越小越暗）、"
            "sound_detected（近 5 秒内是否有声音）、led_state（当前灯状态）、time_period（时段）\n\n"
            f"{_tools_mod.TOOL_DESCRIPTIONS}\n\n"
            f"{history_lines}"
            f"{hints_str}\n\n"
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

    def _find_led_device(self) -> str:
        """从 actuator 快照中查找 LED 设备 ID，找不到返回默认值。"""
        for uid in self._state.actuator_snapshot():
            if uid.endswith("_led"):
                return uid
        return "esp32_desk_led"

    def _get_current_ism(self, led_device: str) -> str:
        """读取 LED 当前 ISM 状态。"""
        snap = self._state.actuator_snapshot()
        return (snap.get(led_device, {}).get("state") or {}).get("ism", "").upper()

    def _execute(self, tool_calls: list, led_device: str):
        """执行 planner 输出的工具调用列表。"""
        from cloud.esp32 import tools as _tools_mod
        now = time.time()
        for call in tool_calls:
            tool_name = call.get("tool", "")
            params = dict(call.get("params", {}))

            if tool_name in ("set_led_state", "set_led_color"):
                params["device_id"] = led_device
                key = f"{tool_name}_{json.dumps(params, sort_keys=True)}"
                if now - self._cooldown.get(key, 0) < 300:
                    if tool_name == "set_led_state":
                        target = params.get("state", "").upper()
                        current = self._get_current_ism(led_device)
                        if current and current == target:
                            logger.debug("cooldown skip: already %s", target)
                            continue
                        else:
                            logger.debug("cooldown skip: %s", key)
                            continue
                    else:
                        logger.debug("cooldown skip: %s", key)
                        continue
                self._cooldown[key] = now
                self._last_act_ts = now

            if tool_name not in _tools_mod.TOOL_FN_MAP:
                logger.warning("unknown tool: %s", tool_name)
                continue
            fn = getattr(_tools_mod, tool_name, None)
            if fn is None:
                logger.warning("unknown tool: %s", tool_name)
                continue
            try:
                fn(**params)
                logger.info("execute %s %s", tool_name, params)
            except Exception as e:
                logger.warning("tool %s failed: %s", tool_name, e)

    def _act(self, belief: dict, combo: str = ""):
        device = "esp32_desk_led"
        now    = time.time()

        state_action = belief.get("state_action")
        color_action = belief.get("color_action")

        current_ism = ""
        for uid, data in self._state.actuator_snapshot().items():
            if uid.endswith("_led"):
                current_ism = (data.get("state") or {}).get("ism", "").upper()
                break

        cmd, params = None, {}
        if state_action and state_action.get("state") == "OFF":
            cmd, params = "SET_STATE", {"state": "OFF"}
        elif state_action and state_action.get("state") == "BRIGHT" and color_action:
            cmd, params = "SET_COLOR", color_action
        elif state_action and state_action.get("state") == "BRIGHT":
            cmd, params = "SET_STATE", {"state": "BRIGHT"}
        elif color_action and current_ism not in ("OFF", "UNKNOWN", ""):
            cmd, params = "SET_COLOR", color_action

        if not cmd:
            return

        key = f"{cmd}_{json.dumps(params, sort_keys=True)}"
        if now - self._cooldown.get(key, 0) < 300:
            if cmd == "SET_STATE":
                target_state = params.get("state", "").upper()
                if current_ism and current_ism != target_state:
                    logger.debug("cooldown bypass: target=%s current=%s, resend", target_state, current_ism)
                    self._cooldown[key] = now
                else:
                    logger.debug("cooldown, skip (%s)", key)
                    return
            else:
                logger.debug("cooldown, skip (%s)", key)
                return

        self._cooldown[key] = now
        self._last_act_ts    = now
        self._last_act_combo = combo

        task_id = f"agent_auto_{int(now)}"
        _tools.publish_task(device, task_id, cmd, params, "agent_auto")
        logger.info("act → %s %s %s", device, cmd, params)

    def _speak(self, text: str, priority: str = "normal"):
        if not text:
            return
        _tools.publish_speech(text, priority)
        logger.info("speech → %s", text)

    def _set_led_mood(self, mood: str):
        _tools.publish_led_mood(mood)

    def _publish_thought(self, text: str):
        if not text:
            return
        _tools.publish_thought(text)

    def _check_proactive(self, sense: dict) -> list[str]:
        now = time.time()
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

        current_level = sense.get("light_level", "")
        if self._last_light_level and current_level != self._last_light_level:
            triggers.append("env_changed")
        self._last_light_level = current_level

        if self._last_act_ts > 0 and (now - self._last_act_ts) >= 300:
            triggers.append("unimproved")

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
        logger.info("sensor automation running")
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
                    logger.info("triggered by %s", unit_id)
                except queue.Empty:
                    if time.time() - _last_any_event_ts > OFFLINE_THRESHOLD:
                        logger.debug("periodic tick: device offline, skip")
                        continue
                    logger.debug("periodic tick")

                sense = self._sense()
                if sense is None:
                    logger.debug("sense: no light data, skip")
                    continue
                logger.debug("sense: %s", sense)

                sense["proactive_hints"] = self._check_proactive(sense)

                self._set_led_mood("thinking")
                tool_calls = self._reason(sense)
                if tool_calls is None:
                    self._set_led_mood("idle")
                    continue

                logger.info("tool_calls (%d): %s", len(tool_calls), json.dumps(tool_calls, ensure_ascii=False))

                self._belief_history.append({
                    "ts": time.time(),
                    "context": f"light={sense['light_level']} sound={sense['sound_detected']} led={sense['led_state']}",
                    "actions": [c["tool"] for c in tool_calls],
                })
                if len(self._belief_history) > 10:
                    self._belief_history.pop(0)

                self._beliefs_since_summary += 1
                if self._beliefs_since_summary >= 5:
                    self._belief_summary = self._summarize_beliefs()
                    self._beliefs_since_summary = 0

                if tool_calls:
                    has_speech = any(c["tool"] == "speak" for c in tool_calls)
                    if has_speech:
                        self._set_led_mood("speaking")
                    led_device = self._find_led_device()
                    self._execute(tool_calls, led_device)

                self._set_led_mood("done")
                time.sleep(2)
                self._set_led_mood("idle")

            except Exception as e:
                logger.error("loop error: %s", e, exc_info=True)
