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
        lux = light_data.get("lux", 0)

        sound_detected = False
        sound_recent = False
        now = time.time()
        for unit_id, data in snap.items():
            if unit_id.endswith("_sound"):
                event = data.get("event", {})
                if event:
                    event_ts = event.get("ts", 0)
                    sound_recent = (now - event_ts) < 5
                    sound_detected = sound_recent
                break

        return {
            "light_level": level,
            "light_lux": lux,
            "sound_detected": sound_detected,
            "sound_recent": sound_recent,
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
            "你是一个桌面空间智能体。根据传感器数据判断当前情境，并决定是否需要调节照明。\n\n"
            f"传感器数据：{json.dumps(sense_data, ensure_ascii=False)}\n"
            f"上一次判断：{last_context}（若无则忽略）"
            f"{history_lines}\n\n"
            "输出 JSON（不含代码块）：\n"
            "{\n"
            '  "context": "一句话描述当前情境",\n'
            '  "space_mood": "专注/空闲/嘈杂/昏暗 中的一个",\n'
            '  "should_act": true/false,\n'
            '  "action": {\n'
            '    "device": "esp32_desk_led",\n'
            '    "cmd": "SET_STATE",\n'
            '    "params": {"state": "BRIGHT"}\n'
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

        while True:
            try:
                event = self._event_queue.get(timeout=30)
                unit_id = event.get("unit_id", "")
                now = time.time()
                if now - _last_event_ts.get(unit_id, 0) < DEBOUNCE_SECS:
                    continue
                _last_event_ts[unit_id] = now
            except queue.Empty:
                pass  # 周期 tick

            sense = self._sense()
            if sense is None:
                continue

            belief = self._reason(sense)
            if belief is None:
                continue

            self._belief_history.append(belief)
            if len(self._belief_history) > 10:
                self._belief_history.pop(0)

            self._act(belief)
