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
        self._last_act_combo: str = ""
        self._last_light_level: str = ""
        self._last_proactive_ts: float = 0.0
        self._belief_summary: str = ""
        self._beliefs_since_summary: int = 0
        self._last_combo: str = ""

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
        for uid, data in self._state.actuator_snapshot().items():
            if uid.endswith("_led"):
                led_state = (data.get("state") or {}).get("ism", "UNKNOWN")
                break

        now_dt      = datetime.datetime.now()
        time_str    = now_dt.strftime("%H:%M")
        hour        = now_dt.hour
        if hour < 6:   time_period = "深夜"
        elif hour < 9: time_period = "清晨"
        elif hour < 12: time_period = "上午"
        elif hour < 18: time_period = "下午"
        elif hour < 21: time_period = "傍晚"
        else:           time_period = "夜间"

        if level in ("DARK", "DIM") and sound_detected:
            combo = "dark_active"
        elif level in ("DARK", "DIM") and not sound_detected:
            combo = "dark_silent"
        elif level in ("NORMAL", "BRIGHT") and sound_detected:
            combo = "normal_active"
        else:
            combo = "normal_silent"

        return {
            "light_level": level, "light_value": value, "light_lux": lux,
            "sound_detected": sound_detected, "sound_recent": sound_detected,
            "led_state": led_state, "time_str": time_str, "time_period": time_period,
            "context_combo": combo,
        }

    def _reason(self, sense_data: dict, proactive_triggers: list | None = None, combo_changed: bool = False) -> dict | None:
        last_context = self._belief_history[-1]["context"] if self._belief_history else ""

        if self._belief_summary:
            history_lines = f"\n历史规律摘要：{self._belief_summary}"
        elif self._belief_history:
            recent = self._belief_history[-3:]
            lines = []
            for b in recent:
                ts_str = time.strftime("%H:%M", time.localtime(b.get("ts", 0)))
                lines.append(f"- {ts_str}: {b.get('context', '')}")
            history_lines = "\n近期状态变化：\n" + "\n".join(lines)
        else:
            history_lines = ""

        proactive_ctx = ""
        if proactive_triggers:
            labels = {
                "long_work":   "用户已连续工作超过 60 分钟",
                "unimproved":  "上次执行动作超过 5 分钟但环境未改善",
                "env_changed": "环境光线档位发生跳变",
            }
            trigger_strs = "、".join(labels.get(t, t) for t in proactive_triggers)
            proactive_ctx = (
                f"\n【主动报告触发】当前满足触发条件：{trigger_strs}。"
                "若你认为有必要，可将 proactive_report 设为 true 并在 speech_text 中说出来，不一定要有 action。"
            )

        combo_changed_str = "true（环境情境已改变）" if combo_changed else "false（情境与上次相同）"

        prompt = (
            f"{AGENT_PERSONA}\n"
            "你负责自动控制桌面 LED 灯，并用符合性格的语言表达决策。\n"
            "light_value 是光线传感器 ADC 原始值（0=最暗，4095=最亮），light_level 是档位，"
            "light_lux 是换算后照度（如有）。\n"
            "led_state 是 LED 当前状态（OFF=关, BRIGHT=亮, DIM=暗）。\n"
            f"当前时段：{sense_data.get('time_period', '')}（{sense_data.get('time_str', '')}），请结合时段判断合理行为。\n"
            f"combo_changed={combo_changed_str}\n\n"
            f"传感器数据：{json.dumps(sense_data, ensure_ascii=False)}\n"
            f"context_combo 含义：dark_active=黑暗中有人，dark_silent=黑暗无人，"
            f"normal_active=正常有人，normal_silent=正常无人。当前：{sense_data.get('context_combo', '')}\n"
            f"上一次判断：{last_context}（若无则忽略）"
            f"{history_lines}"
            f"{proactive_ctx}\n\n"
            "决策规则（按优先级）：\n"
            "1. dark_active → state_action=BRIGHT；若 combo_changed=true 同时输出 color_action\n"
            "2. dark_silent 且灯亮 → state_action=OFF，color_action=null\n"
            "3. dark_silent 且灯关 → 两者均 null\n"
            "4. normal_* 且灯亮 → state_action=OFF，color_action=null\n"
            "5. combo_changed=true 且灯亮 → 可仅输出 color_action 换色，state_action=null\n"
            "6. 其他 → 两者均 null\n\n"
            "颜色参考（结合时段和 combo 自由决定 r/g/b，0-255）：\n"
            "深夜暖橙(255,100,30 亮120)、傍晚暖黄(255,160,60 亮160)、"
            "清晨淡蓝白(200,220,255 亮200)、上午专注冷白(220,220,255 亮220)、"
            "嘈杂活跃偏青(180,220,200 亮200)，不限于此。\n\n"
            "输出 JSON（不含代码块）：\n"
            "{\n"
            '  "context": "一句话描述当前情境（内部日志）",\n'
            '  "space_mood": "专注/空闲/嘈杂/昏暗 中的一个",\n'
            '  "should_act": true/false,\n'
            '  "state_action": {"state": "BRIGHT"} 或 {"state": "OFF"} 或 null,\n'
            '  "color_action": {"r": 0-255, "g": 0-255, "b": 0-255, "brightness": 0-255} 或 null,\n'
            '  "reason": "为什么这样决定（内部日志）",\n'
            '  "speech_text": "智能体说出来的话，简洁不超过两句，不执行动作时可为空字符串",\n'
            '  "thought_text": "有趣的推理，简洁一句话，不值得说就为空字符串",\n'
            '  "should_verbalize_thought": true/false,\n'
            '  "proactive_report": true/false\n'
            "}\n"
        )

        try:
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            content = resp.content.strip()
            start = content.find("{")
            end   = content.rfind("}") + 1
            if start == -1:
                raise ValueError("no JSON found")
            belief = json.loads(content[start:end])
            belief["ts"] = time.time()
            return belief
        except Exception as e:
            logger.warning("reason parse error: %s", e)
            return None

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
        combo = sense.get("context_combo", "")

        if combo.endswith("_active"):
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

        if (self._last_act_ts > 0
                and (now - self._last_act_ts) >= 300
                and combo == self._last_act_combo):
            triggers.append("unimproved")

        return triggers

    def _summarize_beliefs(self) -> str:
        recent = self._belief_history[-5:]
        entries = []
        for b in recent:
            ts_str = time.strftime("%H:%M", time.localtime(b.get("ts", 0)))
            entries.append(f"{ts_str}: {b.get('context', '')}（动作：{b.get('reason', '')}）")
        prompt = "以下是最近的桌面空间状态变化记录，用一句话总结规律（20字以内）：\n" + "\n".join(entries)
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

                proactive_triggers = self._check_proactive(sense)
                combo_changed = sense.get("context_combo", "") != self._last_combo

                self._set_led_mood("thinking")
                belief = self._reason(sense, proactive_triggers or None, combo_changed=combo_changed)
                if belief is None:
                    self._set_led_mood("idle")
                    continue
                logger.info("belief: should_act=%s, reason=%s", belief.get("should_act"), belief.get("reason"))

                self._belief_history.append(belief)
                if len(self._belief_history) > 10:
                    self._belief_history.pop(0)

                self._beliefs_since_summary += 1
                if self._beliefs_since_summary >= 5:
                    self._belief_summary = self._summarize_beliefs()
                    self._beliefs_since_summary = 0

                if belief.get("should_verbalize_thought") and belief.get("thought_text"):
                    self._publish_thought(belief["thought_text"])

                speech_text = belief.get("speech_text", "")
                if speech_text and (combo_changed or proactive_triggers):
                    self._set_led_mood("speaking")
                    self._speak(speech_text)

                if belief.get("proactive_report") and speech_text:
                    self._last_proactive_ts = time.time()

                self._act(belief, combo=sense.get("context_combo", ""))
                self._last_combo = sense.get("context_combo", "")

                self._set_led_mood("done")
                time.sleep(2)
                self._set_led_mood("idle")

            except Exception as e:
                logger.error("loop error: %s", e, exc_info=True)
