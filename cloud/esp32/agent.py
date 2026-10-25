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

LLM_UNAVAILABLE_REPLY = (
    "灯的小脑袋暂时连不上云端，刚才没法认真听懂你的意思。"
    "你可以稍后再和我说，或者先用上面的按钮直接调灯。"
)

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
        self._last_reason_ts: float = 0.0

    def get_autonomy_mode(self) -> str:
        """返回当前自主模式：manual（仅听命令）/ reactive（自发调光）。"""
        return self._autonomy_mode

    def set_autonomy_mode(self, mode: str) -> None:
        """切换自主模式；非法值抛 ValueError。"""
        if mode not in _VALID_AUTONOMY_MODES:
            raise ValueError(f"未知自主模式: {mode}")
        self._autonomy_mode = mode
        if mode == "manual":
            from cloud.peers.gateway import cancel_pending
            cancel_pending(initiator_unit_id=self._find_led_unit(),
                           origin_kind="agent_autonomous", reason="mode_changed")
        if mode == "reactive":
            self._last_reason_sig = None   # 切回自主时清签名，立刻按当前情境评估一次
        else:
            self._user_hold_until = 0.0
        logger.info("autonomy mode → %s", mode)

    def mark_user_command(self, unit_id: str) -> None:
        """自主模式收到外部命令时，开启让位窗口。"""
        if self._autonomy_mode != "reactive":
            return
        self._user_hold_until = time.time() + USER_HOLD_TTL
        from cloud.peers.gateway import cancel_pending
        cancel_pending(initiator_unit_id=unit_id, origin_kind="agent_autonomous",
                       reason="user_takeover")
        logger.info("user command on %s → 自主层让位 %ds", unit_id, USER_HOLD_TTL)

    def wait_task_result(self, task_id: str, timeout: float = 20.0) -> dict:
        """供设备页对话等待 ESP32 的实际执行反馈。"""
        return self._state.wait_task_result(task_id, timeout=timeout)

    def narrate_command(self, action: str, params: dict) -> None:
        """收到用户/编排器命令时，用 persona 生成一句台词并发 thought。

        与 Go2 行为一致：执行后用性格大脑吐一句确认/吐槽，并发布到
        ssm/agents/+/thought。必须能安全地从子线程调用——
        内部全部 try/except 包好，LLM 异常时用兜底台词，绝不向外抛。
        """
        try:
            params = params or {}
            desc_parts = []
            if "state" in params:
                desc_parts.append(f"state={params['state']}")
            for k in ("r", "g", "b", "brightness", "count"):
                if k in params:
                    desc_parts.append(f"{k}={params[k]}")
            params_desc = "、".join(desc_parts) if desc_parts else "无额外参数"

            prompt = (
                f"{AGENT_PERSONA}\n"
                "刚刚有人（用户或编排器）给你下达了一条控制 LED 灯的命令，你已经照做了。\n"
                f"命令动作：{action}（SET_STATE=开关，SET_COLOR=调色温/亮度，BLINK=闪烁）\n"
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

    def handle_user_text(self, text: str, *, persist: bool = True) -> str | dict:
        """理解设备页文本：即时控灯、调教或闲聊；实际动作由 API 派发。"""
        from cloud.esp32.memory import taught
        try:
            peer_view = _tools.list_peers(self._find_led_unit())
        except Exception:
            peer_view = []
        prompt = (
            f"{AGENT_PERSONA}\n"
            "用户对你说了一句话。把当前要执行的灯光指令分类为 act；"
            "教你以后遇到某情境再做某事分类为 teach；明确让其他智能体做事分类为 peer；其他问题或闲聊分类为 chat。\n"
            "act 只能使用以下动作：SET_STATE 开关灯，params.state 为 ON 或 OFF；"
            "SET_COLOR 调色或调亮度，params 含 r、g、b（0-255 整数）和 brightness（0-255 整数）；"
            "BLINK 闪烁，params.count 为 1-20 整数，可选 r、g、b。"
            "用户只要求改变亮度时，请保留当前 RGB；只要求改变颜色时，请保留当前亮度。\n"
            f"当前灯状态：{json.dumps(self._state.actuator_snapshot(), ensure_ascii=False)}\n"
            f"同空间同伴（仅列公开技能）：{json.dumps(peer_view, ensure_ascii=False)}\n"
            "peer 只可选择当前在线同伴卡中已有的技能；请求 Go2 打招呼时用 go2_sport 和 params.cmd=Hello。"
            "不得推测动作已完成，执行结果由系统返回。\n"
            f"用户说：{text}\n\n"
            "只输出 JSON，不含代码块：\n"
            '{"type": "act|teach|peer|chat", "action": "act 时填 SET_STATE|SET_COLOR|BLINK", '
            '"params": {}, "trigger": "teach 时填未来情境", '
            '"behavior": "teach 时填未来行为", "target_unit_id": "peer 时填目标单元", '
            '"skill_id": "peer 时填卡中的技能 ID", "reply": "简短回复；act/peer 时仅在执行成功后展示"}\n'
            '例："打开灯" → {"type":"act","action":"SET_STATE","params":{"state":"ON"},'
            '"reply":"行吧，给你开了。"}；'
            '"调成红色" → {"type":"act","action":"SET_COLOR",'
            '"params":{"r":255,"g":0,"b":0,"brightness":180},"reply":"给你调红了。"}'
        )
        try:
            resp = self._llm.invoke([HumanMessage(content=prompt)])
            content = resp.content.strip()
            content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("`").strip()
            data = json.loads(content[content.find("{"):content.rfind("}") + 1])
            if not isinstance(data, dict):
                raise ValueError("LLM reply is not a JSON object")
        except Exception as e:
            logger.warning("handle_user_text parse error: %s", e)
            return {
                "reply": LLM_UNAVAILABLE_REPLY,
                "llm_available": False,
                "error": "llm_unavailable",
            }

        if data.get("type") == "act":
            return {
                "type": "act",
                "action": str(data.get("action") or "").upper(),
                "params": data.get("params"),
                "reply": data.get("reply") or "行吧，照你说的办了。",
            }
        if data.get("type") == "peer":
            return {
                "type": "peer",
                "target_unit_id": str(data.get("target_unit_id") or ""),
                "skill_id": str(data.get("skill_id") or ""),
                "params": data.get("params") if isinstance(data.get("params"), dict) else {},
                "reply": data.get("reply") or "同伴任务完成了。",
            }
        if data.get("type") == "teach" and data.get("trigger") and data.get("behavior"):
            if not persist:
                return {
                    "reply": "这是临时体验，我可以照做当前指令，但不会保存长期调教。",
                    "persisted": False,
                }
            taught.add(data["trigger"], data["behavior"])
            self._event_queue.put({"unit_id": "taught_rules", "enqueue_ts": time.time()})
            return data.get("reply") or f"学会了：{data['trigger']}就{data['behavior']}"
        if data.get("type") == "chat":
            return data.get("reply") or "好的~"
        return {"result": "error", "reply": "这句话我没理解清楚，请再说一次。"}

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
        led_config = {"r": 0, "g": 0, "b": 0, "brightness": 0, "count": None}
        for uid, data in self._state.actuator_snapshot().items():
            if uid.endswith("_led"):
                state = data.get("state") or {}
                led_state = state.get("ism", "UNKNOWN")
                led_config = {key: state.get(key) for key in led_config}
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
            "led_state": led_state, "led_config": led_config,
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

        try:
            peer_view = _tools_mod.list_peers(self._find_led_unit())
        except Exception:
            peer_view = []
        prompt = (
            f"{AGENT_PERSONA}\n"
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
        now = time.time()
        params = dict(params)

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
            if tool_name == "list_peers":
                _tools_mod.list_peers(led_unit)
                return True
            if tool_name == "request_peer_skill":
                if not isinstance(params.get("params"), dict):
                    return False
                result = _tools_mod.request_peer_skill(
                    target_unit_id=str(params.get("target_unit_id") or ""),
                    skill_id=str(params.get("skill_id") or ""),
                    params=params["params"], unit_id=led_unit,
                    request_id=params.get("request_id"),
                )
                if result.get("result") != "ok":
                    logger.warning("peer request did not complete: %s", result)
                    return False
                self._last_act_ts = time.time()
                return True
            task_id = fn(**params)
            if tool_name in ("set_led_state", "set_led_color", "blink_led"):
                # Real tools return an auto_* task id and must await the edge result.
                # A synchronous adapter may return a completion marker directly.
                if isinstance(task_id, str) and task_id.startswith("auto_"):
                    result = self._state.wait_task_result(task_id)
                    if result.get("result") != "ok":
                        logger.warning("command %s did not complete: %s", task_id, result)
                        return False
                self._cooldown[key] = time.time()
                self._last_act_ts = time.time()
            logger.info("execute %s %s", tool_name, params)
            return True
        except Exception as e:
            logger.warning("tool %s failed: %s", tool_name, e)
            return False

    def _execute(self, tool_calls: list, led_unit: str):
        """执行 planner 输出的工具调用列表（逐个走单一执行口）。

        若某调用带 taught_id 且执行成功，记一次调教命中。
        """
        from cloud.esp32.memory import taught as _taught
        completed = []
        for call in tool_calls:
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

                if not self._should_act():
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
                from cloud.esp32.memory import taught as _taught
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
                self._last_reason_sig = sig   # 这个情境已评估过，下次相同则跳过

                logger.info("tool_calls (%d): %s", len(tool_calls), json.dumps(tool_calls, ensure_ascii=False))

                completed = self._execute(tool_calls, self._find_led_unit()) if tool_calls else []
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
