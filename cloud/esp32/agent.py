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

USER_HOLD_TTL = 300   # 用户/编排器命令后，自主层让位时长（秒）
_VALID_AUTONOMY_MODES = ("manual", "reactive")

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
        self._autonomy_mode: str = "manual"   # 默认手动：连上不自动控灯，reactive 需显式开启
        self._user_hold_until: float = 0.0
        self._last_reason_sig: tuple | None = None   # 上次问 LLM 时的情境签名（实质变化门）

    def get_autonomy_mode(self) -> str:
        """返回当前自主模式：manual（仅听命令）/ reactive（自发调光）。"""
        return self._autonomy_mode

    def set_autonomy_mode(self, mode: str) -> None:
        """切换自主模式；非法值抛 ValueError。"""
        if mode not in _VALID_AUTONOMY_MODES:
            raise ValueError(f"未知自主模式: {mode}")
        self._autonomy_mode = mode
        if mode == "reactive":
            self._last_reason_sig = None   # 切回自主时清签名，立刻按当前情境评估一次
        logger.info("autonomy mode → %s", mode)

    def mark_user_command(self, unit_id: str) -> None:
        """收到非自身发出的命令时调用，开启自主层让位窗口。"""
        self._user_hold_until = time.time() + USER_HOLD_TTL
        logger.info("user command on %s → 自主层让位 %ds", unit_id, USER_HOLD_TTL)

    def narrate_command(self, action: str, params: dict) -> None:
        """收到用户/编排器命令时，用 persona 生成一句台词并发 thought。

        与 Go2 行为一致：执行后用性格大脑吐一句确认/吐槽，PWA 经
        ssm/agents/+/thought 通配渲染。必须能安全地从子线程调用——
        内部全部 try/except 包好，LLM 异常时用兜底台词，绝不向外抛。
        """
        try:
            params = params or {}
            desc_parts = []
            if "state" in params:
                desc_parts.append(f"state={params['state']}")
            for k in ("r", "g", "b", "brightness"):
                if k in params:
                    desc_parts.append(f"{k}={params[k]}")
            params_desc = "、".join(desc_parts) if desc_parts else "无额外参数"

            prompt = (
                f"{AGENT_PERSONA}\n"
                "刚刚有人（用户或编排器）给你下达了一条控制 LED 灯的命令，你已经照做了。\n"
                f"命令动作：{action}（SET_STATE=开关/亮度，SET_COLOR=调色温/亮度）\n"
                f"命令参数：{params_desc}\n\n"
                "用你的性格说一句简短的话，确认或吐槽这次动作。"
                "只输出这一句台词本身，不超过一句，不要代码块、不要解释、不要引号。\n"
                "例如：行吧，给你开了。 / 这就给你调暖点。 / 又要关灯啊，随你。"
            )
            try:
                resp = self._llm.invoke([HumanMessage(content=prompt)])
                text = (resp.content or "").strip()
                text = text.strip("\"'`").strip()
            except Exception as e:
                logger.warning("narrate_command llm error: %s", e)
                text = "行吧，照你说的办了。"
            if not text:
                text = "行吧，照你说的办了。"
            _tools.publish_thought(text, unit_id="esp32_desk_led")
            logger.info("narrate_command %s → %s", action, text)
        except Exception as e:
            logger.warning("narrate_command failed: %s", e)

    def handle_user_text(self, text: str) -> str:
        """处理设备页直达的用户文本：LLM 分类『闲聊/调教』，调教则落库。

        本入口不执行即时命令（仍走 FSM 按钮/MQTT task）。全程 try/except，
        解析失败降级为礼貌回复，绝不抛出。
        """
        from cloud.esp32.memory import taught
        prompt = (
            f"{AGENT_PERSONA}\n"
            "用户对你说了一句话。判断这是『闲聊』，还是『调教』"
            "（教你以后遇到某情境就做某事）。\n"
            f"用户说：{text}\n\n"
            "只输出 JSON，不含代码块：\n"
            '{"type": "teach"或"chat", "trigger": "情境(调教时填，自然语言)", '
            '"behavior": "行为(调教时填，自然语言)", "reply": "你回复用户的一句话"}'
        )
        try:
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            content = resp.content.strip()
            content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("`").strip()
            data = json.loads(content[content.find("{"):content.rfind("}") + 1])
        except Exception as e:
            logger.warning("handle_user_text parse error: %s", e)
            return "我没太听明白，可以再说一遍吗？"

        if data.get("type") == "teach" and data.get("trigger") and data.get("behavior"):
            taught.add(data["trigger"], data["behavior"])
            return data.get("reply") or f"学会了：{data['trigger']}就{data['behavior']}"
        return data.get("reply") or "好的~"

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

        from cloud.esp32.memory import taught as _taught
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

        prompt = (
            f"{AGENT_PERSONA}\n"
            "你负责控制桌面 LED 灯，根据当前传感器观测自主决定是否采取行动。\n\n"
            f"当前观测：{json.dumps(sense_data, ensure_ascii=False)}\n"
            "字段说明：light_level（DARK/DIM/NORMAL/BRIGHT）、light_lux（照度，越小越暗）、"
            "sound_detected（近 5 秒内是否有声音）、led_state（当前灯状态）、time_period（时段）\n\n"
            f"{_tools_mod.TOOL_DESCRIPTIONS}\n\n"
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

    def _dispatch_tool(self, tool_name: str, params: dict, led_device: str) -> bool:
        """单一执行口：冷却判定 → 执行 → 记录。返回是否真正执行。

        set_led_state / set_led_color 共享 self._cooldown（同参 5 分钟内只发一次）；
        speak 等不受冷却约束。色温/亮度由上游 LLM 决定，此处不做任何颜色逻辑。
        """
        from cloud.esp32 import tools as _tools_mod
        now = time.time()
        params = dict(params)

        if tool_name in ("set_led_state", "set_led_color"):
            params["device_id"] = led_device
            key = f"{tool_name}_{json.dumps(params, sort_keys=True)}"
            if now - self._cooldown.get(key, 0) < 300:
                logger.debug("cooldown skip: %s", key)
                return False
            self._cooldown[key] = now
            self._last_act_ts = now

        if tool_name not in _tools_mod.TOOL_FN_MAP:
            logger.warning("unknown tool: %s", tool_name)
            return False
        fn = getattr(_tools_mod, tool_name, None)
        if fn is None:
            logger.warning("unknown tool: %s", tool_name)
            return False
        try:
            fn(**params)
            logger.info("execute %s %s", tool_name, params)
            return True
        except Exception as e:
            logger.warning("tool %s failed: %s", tool_name, e)
            return False

    def _execute(self, tool_calls: list, led_device: str):
        """执行 planner 输出的工具调用列表（逐个走单一执行口）。

        若某调用带 taught_id 且执行成功，记一次调教命中。
        """
        from cloud.esp32.memory import taught as _taught
        for call in tool_calls:
            ok = self._dispatch_tool(call.get("tool", ""), call.get("params", {}), led_device)
            if ok and call.get("taught_id"):
                _taught.touch(call["taught_id"])

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

                if not self._should_act():
                    logger.debug("autonomy gated (mode=%s hold=%s), skip self-action",
                                 self._autonomy_mode, self._in_user_hold())
                    continue

                # 实质变化门：只有慢变量（光照档位 / 时段）变了才值得问 LLM。
                # 声音等高频噪声不进签名，避免反复花配额让 LLM 决定"不用动灯"。
                sig = (sense["light_level"], sense["time_period"])
                if sig == self._last_reason_sig:
                    logger.debug("no material change %s, skip LLM", sig)
                    continue

                tool_calls = self._reason(sense)
                if tool_calls is None:
                    continue
                self._last_reason_sig = sig   # 这个情境已评估过，下次相同则跳过

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
                    led_device = self._find_led_device()
                    self._execute(tool_calls, led_device)

            except Exception as e:
                logger.error("loop error: %s", e, exc_info=True)
