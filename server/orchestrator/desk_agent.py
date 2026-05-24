import os
import json
import queue
import time
import threading

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage


class DeskAgent:
    def __init__(self, shared_state, publish_task_fn, llm=None):
        self._shared_state = shared_state
        self._publish_task = publish_task_fn
        self._llm = llm if llm is not None else self._make_llm()
        self._event_queue: queue.Queue = queue.Queue()
        self._belief_history: list[dict] = []
        self._cooldown: dict[str, float] = {}
        self._last_sound_ts: float = 0.0   # 服务器侧收到声音事件的时刻

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
        t = threading.Thread(target=self._loop, daemon=True, name="DeskAgent")
        t.start()

    def push_sensor_event(self, unit_id: str, payload: dict):
        if unit_id.endswith("_sound"):
            self._last_sound_ts = time.time()   # 用服务器时间，不信任 ESP32 的 ts
        self._event_queue.put({"unit_id": unit_id, "payload": payload})

    def _sense(self) -> dict | None:
        snap = self._shared_state.sensor_snapshot()

        light_data = None
        for unit_id, data in snap.items():
            if unit_id.endswith("_light"):
                light_data = data.get("state") or data.get("event")
                break

        if not light_data:
            return None

        level = light_data.get("level", "NORMAL")
        value = light_data.get("value", 0)  # ESP32 原始 ADC 值

        now = time.time()
        sound_recent = (now - self._last_sound_ts) < 5
        sound_detected = sound_recent

        # 读取 LED 当前 ISM 状态
        led_state = "UNKNOWN"
        actuator_snap = self._shared_state.actuator_snapshot()
        for unit_id, data in actuator_snap.items():
            if unit_id.endswith("_led"):
                state_msg = data.get("state", {})
                led_state = state_msg.get("ism", "UNKNOWN")
                break

        return {
            "light_level": level,
            "light_value": value,
            "sound_detected": sound_detected,
            "sound_recent": sound_recent,
            "led_state": led_state,
        }

    def _reason(self, sense_data: dict) -> dict | None:
        last_context = self._belief_history[-1]["context"] if self._belief_history else ""

        history_lines = ""
        if self._belief_history:
            recent = self._belief_history[-3:]
            lines = []
            for b in recent:
                ts_str = time.strftime("%H:%M", time.localtime(b.get("ts", 0)))
                lines.append(f"- {ts_str}: {b.get('context', '')}")
            history_lines = "\n近期状态变化：\n" + "\n".join(lines)

        prompt = (
            "你是一个桌面空间智能体，负责自动控制桌面 LED 灯。\n"
            "light_value 是光线传感器 ADC 原始值（0=最暗，4095=最亮），light_level 是档位。\n"
            "led_state 是 LED 当前状态（OFF=关, BRIGHT=亮, DIM=暗）。\n\n"
            f"传感器数据：{json.dumps(sense_data, ensure_ascii=False)}\n"
            f"上一次判断：{last_context}（若无则忽略）"
            f"{history_lines}\n\n"
            "决策规则（按优先级）：\n"
            "1. 若 light_level 为 DARK/DIM 且有人活动（sound_recent=true）→ 开灯（BRIGHT）\n"
            "2. 若 light_level 为 DARK/DIM 且 led_state 为 OFF → 开灯（BRIGHT）\n"
            "3. 若 light_level 为 NORMAL/BRIGHT 且 led_state 不是 OFF → 关灯（OFF）\n"
            "4. 其他情况 → 不动作\n\n"
            "输出 JSON（不含代码块），state 只能填 BRIGHT 或 OFF：\n"
            "{\n"
            '  "context": "一句话描述当前情境",\n'
            '  "space_mood": "专注/空闲/嘈杂/昏暗 中的一个",\n'
            '  "should_act": true/false,\n'
            '  "action": {\n'
            '    "device": "esp32_desk_led",\n'
            '    "cmd": "SET_STATE",\n'
            '    "params": {"state": "BRIGHT 或 OFF"}\n'
            "  },\n"
            '  "reason": "为什么这样决定"\n'
            "}\n"
            "若 should_act 为 false，action 填 {}。"
        )

        try:
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            content = resp.content.strip()
            start = content.find("{")
            end = content.rfind("}") + 1
            if start == -1:
                raise ValueError("no JSON found")
            belief = json.loads(content[start:end])
            belief["ts"] = time.time()
            return belief
        except Exception as e:
            print(f"[DeskAgent] reason parse error: {e}")
            return None

    def _act(self, belief: dict):
        if not belief.get("should_act"):
            return
        action = belief.get("action", {})
        if not action:
            return

        device = action.get("device", "")
        cmd = action.get("cmd", "")
        params = action.get("params", {})

        key = f"{cmd}_{json.dumps(params, sort_keys=True)}"
        now = time.time()
        if now - self._cooldown.get(key, 0) < 300:
            print(f"[DeskAgent] cooldown, skip ({key})")
            return
        self._cooldown[key] = now

        task_id = f"agent_auto_{int(now)}"
        self._publish_task(device, task_id, cmd, params, "agent_auto")
        print(f"[DeskAgent] act → {device} {cmd} {params}")

    def _loop(self):
        print("[DeskAgent] running")
        _last_event_ts: dict[str, float] = {}
        DEBOUNCE_SECS = 5
        OFFLINE_THRESHOLD = 120  # 超过 120s 没收到任何传感器事件，视为设备离线
        _last_any_event_ts: float = 0.0

        while True:
            try:
                triggered_by_event = False
                try:
                    event = self._event_queue.get(timeout=30)
                    unit_id = event.get("unit_id", "")
                    now = time.time()
                    _last_any_event_ts = now
                    if now - _last_event_ts.get(unit_id, 0) < DEBOUNCE_SECS:
                        continue
                    _last_event_ts[unit_id] = now
                    triggered_by_event = True
                    print(f"[DeskAgent] triggered by {unit_id}")
                except queue.Empty:
                    # 周期 tick：设备离线时不调 LLM
                    if time.time() - _last_any_event_ts > OFFLINE_THRESHOLD:
                        print("[DeskAgent] periodic tick: device offline, skip")
                        continue
                    print("[DeskAgent] periodic tick")

                sense = self._sense()
                if sense is None:
                    print("[DeskAgent] sense: no light data, skip")
                    continue
                print(f"[DeskAgent] sense: {sense}")

                belief = self._reason(sense)
                if belief is None:
                    continue
                print(f"[DeskAgent] belief: should_act={belief.get('should_act')}, reason={belief.get('reason')}")

                self._belief_history.append(belief)
                if len(self._belief_history) > 10:
                    self._belief_history.pop(0)

                self._act(belief)
            except Exception as e:
                print(f"[DeskAgent] loop error: {e}")
