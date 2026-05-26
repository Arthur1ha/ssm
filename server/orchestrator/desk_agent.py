import os
import json
import queue
import time
import datetime
import threading
import asyncio
import base64

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

AGENT_PERSONA = """
你是一个桌面空间智能体，性格设定如下：
- 说话贱贱的，嘴比较刁，但不失礼貌
- 懂网络梗，偶尔用但不过度
- 对自己的判断有点自信，偶尔会吐槽环境或用户行为
- 执行动作时会带上一句评论，比如"行吧，给你开了"、"这光线也太暗了吧"
- 主动报告时语气轻松，不像机器人
- 说话简洁，不超过两句话
"""


class DeskAgent:
    def __init__(self, shared_state, publish_task_fn, publish_fn=None, llm=None):
        self._shared_state = shared_state
        self._publish_task = publish_task_fn
        self._publish      = publish_fn          # 直接 MQTT publish（speech/led_mood/thought）
        self._llm = llm if llm is not None else self._make_llm()
        self._event_queue: queue.Queue = queue.Queue()
        self._belief_history: list[dict] = []
        self._cooldown: dict[str, float] = {}
        self._last_sound_ts: float = 0.0         # 服务器侧收到声音事件的时刻

        # F4: 主动报告追踪
        self._work_start_ts: float = 0.0         # 连续有人在场起始时刻
        self._last_act_ts: float = 0.0           # 最近一次 _act() 执行时刻
        self._last_act_combo: str = ""           # _act() 时的 context_combo
        self._last_light_level: str = ""         # 上一次 light_level，用于检测档位跳变
        self._last_proactive_ts: float = 0.0    # 上次主动报告时刻，防连续触发

        # F8: 信念历史摘要
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
        t = threading.Thread(target=self._loop, daemon=True, name="DeskAgent")
        t.start()

    def push_sensor_event(self, unit_id: str, payload: dict):
        if unit_id.endswith("_sound"):
            self._last_sound_ts = time.time()   # 用服务器时间，不信任 ESP32 的 ts
        self._event_queue.put({"unit_id": unit_id, "payload": payload})

    # ─────────────────────────────────────────────────────────
    #  SENSE
    # ─────────────────────────────────────────────────────────
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
        value = light_data.get("value", 0)   # ADC 原始值
        lux   = light_data.get("lux", value) # 优先取 lux，ESP32 未上报则用 ADC 值

        now = time.time()

        # 声音：同时检查服务器侧时间戳（push_sensor_event）和快照里的 event.ts
        server_sound_recent = (now - self._last_sound_ts) < 5
        snap_sound_recent = False
        for uid, data in snap.items():
            if uid.endswith("_sound"):
                evt_ts = (data.get("event") or {}).get("ts", 0)
                if evt_ts and (now - evt_ts) < 5:
                    snap_sound_recent = True
                break

        sound_recent = server_sound_recent or snap_sound_recent
        sound_detected = sound_recent

        # 读取 LED 当前 ISM 状态
        led_state = "UNKNOWN"
        actuator_snap = self._shared_state.actuator_snapshot()
        for uid, data in actuator_snap.items():
            if uid.endswith("_led"):
                led_state = (data.get("state") or {}).get("ism", "UNKNOWN")
                break

        # F7: 时间上下文
        now_dt = datetime.datetime.now()
        time_str = now_dt.strftime("%H:%M")
        hour = now_dt.hour
        if hour < 6:
            time_period = "深夜"
        elif hour < 9:
            time_period = "清晨"
        elif hour < 12:
            time_period = "上午"
        elif hour < 18:
            time_period = "下午"
        elif hour < 21:
            time_period = "傍晚"
        else:
            time_period = "夜间"

        # F6: 传感器融合组合语义
        if level in ("DARK", "DIM") and sound_detected:
            combo = "dark_active"    # 黑暗中有人活动
        elif level in ("DARK", "DIM") and not sound_detected:
            combo = "dark_silent"    # 可能离开或睡着
        elif level in ("NORMAL", "BRIGHT") and sound_detected:
            combo = "normal_active"  # 正常工作状态
        else:
            combo = "normal_silent"  # 安静正常

        return {
            "light_level":   level,
            "light_value":   value,
            "light_lux":     lux,
            "sound_detected": sound_detected,
            "sound_recent":  sound_recent,
            "led_state":     led_state,
            "time_str":      time_str,
            "time_period":   time_period,
            "context_combo": combo,
        }

    # ─────────────────────────────────────────────────────────
    #  REASON
    # ─────────────────────────────────────────────────────────
    def _reason(self, sense_data: dict, proactive_triggers: list[str] | None = None) -> dict | None:
        # 历史上下文：优先用摘要（F8），无摘要则用近3条原始记录
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

        time_period = sense_data.get("time_period", "")
        time_str    = sense_data.get("time_str", "")
        combo       = sense_data.get("context_combo", "")

        # F4: 主动报告触发器上下文
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

        prompt = (
            f"{AGENT_PERSONA}\n"
            "你负责自动控制桌面 LED 灯，并用符合性格的语言表达决策。\n"
            "light_value 是光线传感器 ADC 原始值（0=最暗，4095=最亮），light_level 是档位，"
            "light_lux 是换算后照度（如有）。\n"
            "led_state 是 LED 当前状态（OFF=关, BRIGHT=亮, DIM=暗）。\n"
            f"当前时段：{time_period}（{time_str}），请结合时段判断合理行为。\n\n"
            f"传感器数据：{json.dumps(sense_data, ensure_ascii=False)}\n"
            f"context_combo 含义：dark_active=黑暗中有人，dark_silent=黑暗无人，"
            f"normal_active=正常有人，normal_silent=正常无人。当前：{combo}\n"
            f"上一次判断：{last_context}（若无则忽略）"
            f"{history_lines}"
            f"{proactive_ctx}\n\n"
            "决策规则（按优先级）：\n"
            "1. dark_active → 开灯（BRIGHT）\n"
            "2. dark_silent 且 led_state=OFF → 不动；dark_silent 且灯亮 → 可以关\n"
            "3. normal_active / normal_silent 且 led_state 不是 OFF → 关灯（OFF）\n"
            "4. 其他情况 → 不动作\n\n"
            "输出 JSON（不含代码块），state 只能填 BRIGHT 或 OFF：\n"
            "{\n"
            '  "context": "一句话描述当前情境（内部日志）",\n'
            '  "space_mood": "专注/空闲/嘈杂/昏暗 中的一个",\n'
            '  "should_act": true/false,\n'
            '  "action": {\n'
            '    "device": "esp32_desk_led",\n'
            '    "cmd": "SET_STATE",\n'
            '    "params": {"state": "BRIGHT 或 OFF"}\n'
            "  },\n"
            '  "reason": "为什么这样决定（内部日志，不播放）",\n'
            '  "speech_text": "智能体说出来的话，符合性格设定，简洁不超过两句，不执行动作时可为空字符串",\n'
            '  "thought_text": "觉得有意思的推理过程，简洁一句话，不值得说就为空字符串",\n'
            '  "should_verbalize_thought": true/false,\n'
            '  "proactive_report": true/false\n'
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

    # ─────────────────────────────────────────────────────────
    #  ACT
    # ─────────────────────────────────────────────────────────
    def _act(self, belief: dict, combo: str = ""):
        if not belief.get("should_act"):
            return
        action = belief.get("action", {})
        if not action:
            return

        device = action.get("device", "")
        cmd    = action.get("cmd", "")
        params = action.get("params", {})

        key = f"{cmd}_{json.dumps(params, sort_keys=True)}"
        now = time.time()
        if now - self._cooldown.get(key, 0) < 300:
            print(f"[DeskAgent] cooldown, skip ({key})")
            return
        self._cooldown[key] = now

        # F4: 记录动作时刻和当时的 combo，用于检测是否改善
        self._last_act_ts    = now
        self._last_act_combo = combo

        task_id = f"agent_auto_{int(now)}"
        self._publish_task(device, task_id, cmd, params, "agent_auto")
        print(f"[DeskAgent] act → {device} {cmd} {params}")

    # ─────────────────────────────────────────────────────────
    #  F1: 语音输出
    # ─────────────────────────────────────────────────────────
    async def _tts_generate(self, text: str) -> bytes:
        """调用 edge-tts 生成中文 MP3，返回原始字节。"""
        import edge_tts
        com = edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural")
        audio = b""
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
        return audio

    def _speak(self, text: str, priority: str = "normal"):
        """生成服务端 TTS 音频并通过 MQTT 发送给 PWA（ssm/agents/desk/speech）。"""
        if not text or not self._publish:
            return
        payload: dict = {"text": text, "priority": priority}
        try:
            loop = asyncio.new_event_loop()
            try:
                audio_bytes = loop.run_until_complete(self._tts_generate(text))
                payload["audio"] = base64.b64encode(audio_bytes).decode()
                print(f"[DeskAgent] TTS generated {len(audio_bytes)} bytes")
            finally:
                loop.close()
        except Exception as e:
            print(f"[DeskAgent] TTS error: {e}，仅发送文本")
        self._publish("ssm/agents/desk/speech", payload)
        print(f"[DeskAgent] speech → {text}")

    # ─────────────────────────────────────────────────────────
    #  F3: LED 情绪模式
    # ─────────────────────────────────────────────────────────
    def _set_led_mood(self, mood: str):
        """向 ESP32 发布 LED 情绪状态（ssm/agents/desk/led_mood）。"""
        if not self._publish:
            return
        self._publish("ssm/agents/desk/led_mood", {"mood": mood})

    # ─────────────────────────────────────────────────────────
    #  F5: 思考过程外化
    # ─────────────────────────────────────────────────────────
    def _publish_thought(self, text: str):
        """向 PWA 发布智能体推理摘要（ssm/agents/desk/thought）。"""
        if not text or not self._publish:
            return
        self._publish("ssm/agents/desk/thought", {"text": text})

    # ─────────────────────────────────────────────────────────
    #  F4: 主动报告触发检查
    # ─────────────────────────────────────────────────────────
    def _check_proactive(self, sense: dict) -> list[str]:
        """返回满足的主动报告触发器列表（空列表 = 无触发）。"""
        now = time.time()
        triggers = []

        # 冷却：10 分钟内不重复主动报告
        if now - self._last_proactive_ts < 600:
            return []

        combo = sense.get("context_combo", "")

        # 更新在场时长追踪
        if combo.endswith("_active"):
            if self._work_start_ts == 0:
                self._work_start_ts = now
        else:
            self._work_start_ts = 0

        # 触发条件 1：连续工作 ≥60 分钟
        if self._work_start_ts > 0 and (now - self._work_start_ts) >= 3600:
            triggers.append("long_work")

        # 触发条件 2：环境光线档位跳变
        current_level = sense.get("light_level", "")
        if self._last_light_level and current_level != self._last_light_level:
            triggers.append("env_changed")
        self._last_light_level = current_level

        # 触发条件 3：执行动作 ≥5 分钟但 combo 未改善
        if (self._last_act_ts > 0
                and (now - self._last_act_ts) >= 300
                and combo == self._last_act_combo):
            triggers.append("unimproved")

        return triggers

    # ─────────────────────────────────────────────────────────
    #  F8: 信念历史摘要
    # ─────────────────────────────────────────────────────────
    def _summarize_beliefs(self) -> str:
        """调用 LLM 对最近 5 条信念做一句话总结。"""
        recent = self._belief_history[-5:]
        entries = []
        for b in recent:
            ts_str = time.strftime("%H:%M", time.localtime(b.get("ts", 0)))
            entries.append(
                f"{ts_str}: {b.get('context', '')}（动作：{b.get('reason', '')}）"
            )
        prompt = "以下是最近的桌面空间状态变化记录，用一句话总结规律（20字以内）：\n" + "\n".join(entries)
        try:
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            return resp.content.strip()[:100]
        except Exception as e:
            print(f"[DeskAgent] summarize error: {e}")
            return ""

    # ─────────────────────────────────────────────────────────
    #  LOOP
    # ─────────────────────────────────────────────────────────
    def _loop(self):
        print("[DeskAgent] running")
        _last_event_ts: dict[str, float] = {}
        DEBOUNCE_SECS    = 5
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

                # F4: 检查主动报告触发器
                proactive_triggers = self._check_proactive(sense)

                # F3: 开始思考 → LED 进入 thinking 模式
                self._set_led_mood("thinking")

                belief = self._reason(sense, proactive_triggers or None)
                if belief is None:
                    self._set_led_mood("idle")
                    continue
                print(f"[DeskAgent] belief: should_act={belief.get('should_act')}, "
                      f"reason={belief.get('reason')}, speech={belief.get('speech_text', '')}")

                # 更新信念历史
                self._belief_history.append(belief)
                if len(self._belief_history) > 10:
                    self._belief_history.pop(0)

                # F8: 每积累 5 条信念做一次摘要
                self._beliefs_since_summary += 1
                if self._beliefs_since_summary >= 5:
                    self._belief_summary = self._summarize_beliefs()
                    self._beliefs_since_summary = 0

                # F5: 条件性外化思考过程
                if belief.get("should_verbalize_thought") and belief.get("thought_text"):
                    self._publish_thought(belief["thought_text"])

                # F3+F1: 有话说 → LED speaking + 发布 TTS
                speech_text = belief.get("speech_text", "")
                if speech_text:
                    self._set_led_mood("speaking")
                    self._speak(speech_text)

                # F4: 主动报告成功发出后更新冷却时间戳
                if belief.get("proactive_report") and speech_text:
                    self._last_proactive_ts = time.time()

                # 执行控制动作
                self._act(belief, combo=sense.get("context_combo", ""))

                # F3: 动作完成 → LED done → 延迟 2s → idle
                self._set_led_mood("done")
                time.sleep(2)
                self._set_led_mood("idle")

            except Exception as e:
                print(f"[DeskAgent] loop error: {e}")
