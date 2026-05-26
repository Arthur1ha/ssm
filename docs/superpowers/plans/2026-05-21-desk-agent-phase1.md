# DeskAgent Phase 1 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在云端构建 DeskAgent，实现感知→推理→执行内部循环，无需用户指令自主调节桌面 LED 照明。

**Architecture:** DeskAgent 独立线程运行（`threading.Thread`），通过内部 `queue.Queue` 接收来自 main.py 的传感器事件，调用 LLM 推理后通过已有的 `do_publish_task` 控制 LED。与 RuleEngine 并行共存互不干扰，5 分钟冷却防止重复触发，最多保留 10 条 belief 历史供推理参考。

**Tech Stack:** Python 3.13, LangChain/ChatOpenAI, threading, queue, pytest

**规范：**
- 运行命令用 `uv run pytest` 而非直接 `pytest`
- 工作目录：`/root/ssm/server/orchestrator/`

---

## 文件地图

| 操作 | 路径 | 职责 |
|------|------|------|
| 新建 | `server/orchestrator/desk_agent.py` | DeskAgent 类全部逻辑 |
| 新建 | `server/orchestrator/tests/__init__.py` | 空文件，使 tests 成为 package |
| 新建 | `server/orchestrator/tests/conftest.py` | sys.path 配置 |
| 新建 | `server/orchestrator/tests/test_desk_agent.py` | 所有单元测试 |
| 修改 | `server/orchestrator/main.py` | 初始化 DeskAgent，转发传感器事件 |
| 修改 | `pyproject.toml` | 添加 pytest 开发依赖 |

---

## Task 1：测试环境搭建

**Files:**
- Modify: `pyproject.toml`
- Create: `server/orchestrator/tests/__init__.py`
- Create: `server/orchestrator/tests/conftest.py`

- [ ] **Step 1：添加 pytest 依赖**

```bash
cd /root/ssm && uv add --dev pytest
```

预期输出包含：`Resolved ... packages`

- [ ] **Step 2：验证 pytest 可用**

```bash
cd /root/ssm && uv run pytest --version
```

预期输出：`pytest 8.x.x`

- [ ] **Step 3：创建 tests 目录和 `__init__.py`**

```bash
mkdir -p /root/ssm/server/orchestrator/tests
touch /root/ssm/server/orchestrator/tests/__init__.py
```

- [ ] **Step 4：创建 `conftest.py` 配置 sys.path**

新建 `server/orchestrator/tests/conftest.py`：

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
```

- [ ] **Step 5：写一个占位测试确认环境正常**

新建 `server/orchestrator/tests/test_desk_agent.py`：

```python
def test_env():
    assert True
```

- [ ] **Step 6：运行占位测试**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py -v
```

预期输出：`1 passed`

- [ ] **Step 7：提交**

```bash
cd /root/ssm && git add pyproject.toml uv.lock server/orchestrator/tests/
git commit -m "chore: 添加 pytest 测试环境"
```

---

## Task 2：DeskAgent 骨架

**Files:**
- Create: `server/orchestrator/desk_agent.py`
- Modify: `server/orchestrator/tests/test_desk_agent.py`

- [ ] **Step 1：写失败测试**

替换 `server/orchestrator/tests/test_desk_agent.py` 全部内容为：

```python
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import MagicMock
import queue
from desk_agent import DeskAgent


def make_agent(llm=None):
    return DeskAgent(
        shared_state=MagicMock(),
        publish_task_fn=MagicMock(),
        llm=llm or MagicMock(),
    )


class TestSkeleton:
    def test_instantiates(self):
        agent = make_agent()
        assert agent is not None

    def test_has_start_method(self):
        agent = make_agent()
        assert callable(agent.start)

    def test_has_push_sensor_event_method(self):
        agent = make_agent()
        assert callable(agent.push_sensor_event)

    def test_belief_history_starts_empty(self):
        agent = make_agent()
        assert agent._belief_history == []

    def test_cooldown_starts_empty(self):
        agent = make_agent()
        assert agent._cooldown == {}

    def test_push_sensor_event_puts_to_queue(self):
        agent = make_agent()
        agent.push_sensor_event("esp32_desk_light", {"level": "DARK"})
        assert agent._event_queue.qsize() == 1
```

- [ ] **Step 2：运行以确认失败**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py -v
```

预期：`ImportError: No module named 'desk_agent'`

- [ ] **Step 3：实现 DeskAgent 骨架**

新建 `server/orchestrator/desk_agent.py`：

```python
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
        pass  # Task 3

    def _reason(self, sense_data: dict) -> dict | None:
        pass  # Task 4

    def _act(self, belief: dict):
        pass  # Task 5

    def _loop(self):
        pass  # Task 6
```

- [ ] **Step 4：运行以确认通过**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py::TestSkeleton -v
```

预期：`6 passed`

- [ ] **Step 5：提交**

```bash
cd /root/ssm && git add server/orchestrator/desk_agent.py server/orchestrator/tests/test_desk_agent.py
git commit -m "feat: DeskAgent 骨架"
```

---

## Task 3：感知层 `_sense()`

**Files:**
- Modify: `server/orchestrator/desk_agent.py`
- Modify: `server/orchestrator/tests/test_desk_agent.py`

SharedState.sensor_snapshot() 返回格式：
```json
{
  "esp32_desk_light": {"state": {"level": "DARK", "lux": 50, "ts": 1000}, "event": {...}},
  "esp32_desk_sound": {"event": {"ts": 1001}}
}
```

- [ ] **Step 1：在测试文件末尾追加 TestSense 类**

在 `server/orchestrator/tests/test_desk_agent.py` 末尾追加：

```python
class TestSense:
    def _make_agent_with_snapshot(self, snapshot: dict):
        state = MagicMock()
        state.sensor_snapshot.return_value = snapshot
        return DeskAgent(shared_state=state, publish_task_fn=MagicMock(), llm=MagicMock())

    def test_returns_none_when_no_light_data(self):
        agent = self._make_agent_with_snapshot({})
        assert agent._sense() is None

    def test_returns_light_level_dark(self):
        snap = {
            "esp32_desk_light": {
                "state": {"level": "DARK", "lux": 50, "ts": 1000},
            }
        }
        agent = self._make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result is not None
        assert result["light_level"] == "DARK"
        assert result["light_lux"] == 50

    def test_falls_back_to_event_when_no_state(self):
        snap = {
            "esp32_desk_light": {
                "event": {"level": "DIM", "lux": 120, "ts": 1000},
            }
        }
        agent = self._make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result["light_level"] == "DIM"

    def test_sound_recent_true_within_5s(self):
        now = time.time()
        snap = {
            "esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": now}},
            "esp32_desk_sound": {"event": {"ts": now - 2}},
        }
        agent = self._make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result["sound_recent"] is True

    def test_sound_recent_false_older_than_5s(self):
        now = time.time()
        snap = {
            "esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": now}},
            "esp32_desk_sound": {"event": {"ts": now - 10}},
        }
        agent = self._make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result["sound_recent"] is False

    def test_no_sound_sensor(self):
        snap = {
            "esp32_desk_light": {"state": {"level": "NORMAL", "lux": 300, "ts": 1000}},
        }
        agent = self._make_agent_with_snapshot(snap)
        result = agent._sense()
        assert result["sound_detected"] is False
        assert result["sound_recent"] is False
```

- [ ] **Step 2：运行以确认失败**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py::TestSense -v
```

预期：`FAILED — _sense() returns None (pass 占位符)`

- [ ] **Step 3：实现 `_sense()`**

替换 `desk_agent.py` 中的 `_sense` 方法：

```python
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
```

- [ ] **Step 4：运行以确认通过**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py::TestSense -v
```

预期：`6 passed`

- [ ] **Step 5：提交**

```bash
cd /root/ssm && git add server/orchestrator/desk_agent.py server/orchestrator/tests/test_desk_agent.py
git commit -m "feat: DeskAgent _sense() 感知层"
```

---

## Task 4：推理层 `_reason()`

**Files:**
- Modify: `server/orchestrator/desk_agent.py`
- Modify: `server/orchestrator/tests/test_desk_agent.py`

- [ ] **Step 1：在测试文件末尾追加 TestReason 类**

```python
import time as _time
from langchain_core.messages import AIMessage


class TestReason:
    SENSE_DATA = {
        "light_level": "DARK",
        "light_lux": 30,
        "sound_detected": False,
        "sound_recent": False,
    }

    def _make_agent_with_llm_response(self, content: str):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(content=content)
        return DeskAgent(shared_state=MagicMock(), publish_task_fn=None, llm=mock_llm)

    def test_returns_belief_dict(self):
        agent = self._make_agent_with_llm_response(
            '{"context": "光线昏暗", "space_mood": "昏暗", "should_act": true, '
            '"action": {"device": "esp32_desk_led", "cmd": "SET_STATE", "params": {"state": "BRIGHT"}}, '
            '"reason": "光线不足"}'
        )
        belief = agent._reason(self.SENSE_DATA)
        assert belief is not None
        assert belief["should_act"] is True
        assert belief["action"]["cmd"] == "SET_STATE"
        assert "ts" in belief

    def test_returns_none_on_invalid_json(self):
        agent = self._make_agent_with_llm_response("not valid json at all")
        belief = agent._reason(self.SENSE_DATA)
        assert belief is None

    def test_handles_json_with_preamble(self):
        agent = self._make_agent_with_llm_response(
            '好的，以下是结果：\n{"context": "正常", "space_mood": "空闲", '
            '"should_act": false, "action": {}, "reason": "ok"}'
        )
        belief = agent._reason(self.SENSE_DATA)
        assert belief is not None
        assert belief["should_act"] is False

    def test_prompt_includes_history_context(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(
            content='{"context": "test", "space_mood": "空闲", "should_act": false, "action": {}, "reason": "ok"}'
        )
        agent = DeskAgent(shared_state=MagicMock(), publish_task_fn=None, llm=mock_llm)
        agent._belief_history = [
            {"context": "空间安静", "space_mood": "空闲", "ts": _time.time() - 120},
            {"context": "有人进入", "space_mood": "专注", "ts": _time.time() - 60},
        ]
        agent._reason(self.SENSE_DATA)
        call_args = mock_llm.invoke.call_args[0][0]
        prompt_text = call_args[0].content
        assert "空间安静" in prompt_text
        assert "有人进入" in prompt_text
```

- [ ] **Step 2：运行以确认失败**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py::TestReason -v
```

预期：`FAILED — _reason() returns None`

- [ ] **Step 3：实现 `_reason()`**

替换 `desk_agent.py` 中的 `_reason` 方法：

```python
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
```

- [ ] **Step 4：运行以确认通过**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py::TestReason -v
```

预期：`4 passed`

- [ ] **Step 5：提交**

```bash
cd /root/ssm && git add server/orchestrator/desk_agent.py server/orchestrator/tests/test_desk_agent.py
git commit -m "feat: DeskAgent _reason() 推理层"
```

---

## Task 5：执行层 `_act()` + 冷却

**Files:**
- Modify: `server/orchestrator/desk_agent.py`
- Modify: `server/orchestrator/tests/test_desk_agent.py`

- [ ] **Step 1：在测试文件末尾追加 TestAct 类**

```python
class TestAct:
    def _make_agent(self):
        mock_publish = MagicMock()
        agent = DeskAgent(shared_state=MagicMock(), publish_task_fn=mock_publish, llm=MagicMock())
        return agent, mock_publish

    def _belief(self, should_act=True, cmd="SET_STATE", params=None):
        return {
            "should_act": should_act,
            "action": {
                "device": "esp32_desk_led",
                "cmd": cmd,
                "params": params or {"state": "BRIGHT"},
            } if should_act else {},
        }

    def test_publishes_when_should_act_true(self):
        agent, mock_publish = self._make_agent()
        agent._act(self._belief())
        mock_publish.assert_called_once()
        args = mock_publish.call_args[0]
        assert args[0] == "esp32_desk_led"
        assert args[2] == "SET_STATE"
        assert args[4] == "agent_auto"

    def test_skips_when_should_act_false(self):
        agent, mock_publish = self._make_agent()
        agent._act(self._belief(should_act=False))
        mock_publish.assert_not_called()

    def test_cooldown_blocks_second_call(self):
        agent, mock_publish = self._make_agent()
        belief = self._belief()
        agent._act(belief)   # first call — publishes
        agent._act(belief)   # second call — same cmd+params, should be blocked
        mock_publish.assert_called_once()

    def test_cooldown_allows_different_params(self):
        agent, mock_publish = self._make_agent()
        agent._act(self._belief(params={"state": "BRIGHT"}))
        agent._act(self._belief(params={"state": "OFF"}))
        assert mock_publish.call_count == 2

    def test_cooldown_log_message(self, capsys):
        agent, _ = self._make_agent()
        belief = self._belief()
        agent._act(belief)
        agent._act(belief)
        captured = capsys.readouterr()
        assert "cooldown" in captured.out
```

- [ ] **Step 2：运行以确认失败**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py::TestAct -v
```

预期：`FAILED — _act() is a no-op`

- [ ] **Step 3：实现 `_act()`**

替换 `desk_agent.py` 中的 `_act` 方法：

```python
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
```

- [ ] **Step 4：运行以确认通过**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py::TestAct -v
```

预期：`5 passed`

- [ ] **Step 5：提交**

```bash
cd /root/ssm && git add server/orchestrator/desk_agent.py server/orchestrator/tests/test_desk_agent.py
git commit -m "feat: DeskAgent _act() 执行层 + 冷却机制"
```

---

## Task 6：主循环 `_loop()` + 短期记忆

**Files:**
- Modify: `server/orchestrator/desk_agent.py`
- Modify: `server/orchestrator/tests/test_desk_agent.py`

- [ ] **Step 1：在测试文件末尾追加 TestLoop 类**

```python
class TestLoop:
    def test_history_appended_after_reason(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(
            content='{"context": "test", "space_mood": "空闲", "should_act": false, "action": {}, "reason": "ok"}'
        )
        agent = DeskAgent(shared_state=MagicMock(), publish_task_fn=MagicMock(), llm=mock_llm)
        sense = {"light_level": "NORMAL", "light_lux": 200, "sound_detected": False, "sound_recent": False}

        belief = agent._reason(sense)
        assert belief is not None
        agent._belief_history.append(belief)
        assert len(agent._belief_history) == 1
        assert agent._belief_history[0]["context"] == "test"

    def test_history_capped_at_10(self):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = AIMessage(
            content='{"context": "x", "space_mood": "空闲", "should_act": false, "action": {}, "reason": "ok"}'
        )
        agent = DeskAgent(shared_state=MagicMock(), publish_task_fn=MagicMock(), llm=mock_llm)
        sense = {"light_level": "NORMAL", "light_lux": 200, "sound_detected": False, "sound_recent": False}

        for _ in range(15):
            b = agent._reason(sense)
            if b:
                agent._belief_history.append(b)
                if len(agent._belief_history) > 10:
                    agent._belief_history.pop(0)

        assert len(agent._belief_history) == 10
```

- [ ] **Step 2：运行以确认通过（这两个测试已经可以通过，因为测试的是已实现的逻辑）**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py::TestLoop -v
```

预期：`2 passed`

- [ ] **Step 3：实现 `_loop()`**

替换 `desk_agent.py` 中的 `_loop` 方法：

```python
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
```

- [ ] **Step 4：运行全部测试**

```bash
cd /root/ssm && uv run pytest server/orchestrator/tests/test_desk_agent.py -v
```

预期：`全部 passed`（TestSkeleton 6 + TestSense 6 + TestReason 4 + TestAct 5 + TestLoop 2 = 23 passed）

- [ ] **Step 5：提交**

```bash
cd /root/ssm && git add server/orchestrator/desk_agent.py server/orchestrator/tests/test_desk_agent.py
git commit -m "feat: DeskAgent _loop() 主循环 + 短期记忆"
```

---

## Task 7：main.py 集成

**Files:**
- Modify: `server/orchestrator/main.py`

这里是手动集成，无单元测试（涉及 MQTT 连接，适合人工验证）。

- [ ] **Step 1：在 main.py 中导入并初始化 DeskAgent**

在 `server/orchestrator/main.py` 找到这一行：

```python
rule_engine  = RuleEngine(state, agent_tools.do_publish_task)
```

在其**后面**追加：

```python
from desk_agent import DeskAgent
desk_agent = DeskAgent(state, agent_tools.do_publish_task, None)
desk_agent.start()
```

- [ ] **Step 2：在传感器事件分支中转发事件**

在 `server/orchestrator/main.py` 找到这一段：

```python
    # 传感器事件：规则引擎处理，不走 LLM
    if trigger == "sensor":
        print(f"[Main] RuleEngine for unit={unit_id}")
        rule_engine.match_and_fire(unit_id, event["payload"])
        continue
```

替换为：

```python
    # 传感器事件：规则引擎处理，不走 LLM；同时通知 DeskAgent
    if trigger == "sensor":
        print(f"[Main] RuleEngine for unit={unit_id}")
        rule_engine.match_and_fire(unit_id, event["payload"])
        desk_agent.push_sensor_event(unit_id, event["payload"])
        continue
```

- [ ] **Step 3：验证语法无误**

```bash
cd /root/ssm && uv run python -c "import sys; sys.path.insert(0,'server/orchestrator'); import main" 2>&1 | head -5
```

预期：无 `SyntaxError`（会有 MQTT 连接尝试，Ctrl+C 中断即可）

- [ ] **Step 4：人工验证——启动服务观察日志**

```bash
cd /root/ssm/server/orchestrator && uv run python main.py 2>&1 | head -20
```

预期日志中出现：
```
[DeskAgent] running
[Main] LangGraph agents ready. Waiting for MQTT events...
```

- [ ] **Step 5：提交**

```bash
cd /root/ssm && git add server/orchestrator/main.py
git commit -m "feat: main.py 集成 DeskAgent，传感器事件双路分发"
```

---

## 验收清单

- [ ] `uv run pytest server/orchestrator/tests/test_desk_agent.py` 全部通过（≥23 个测试）
- [ ] 服务启动后日志出现 `[DeskAgent] running`
- [ ] 遮住光线传感器后（无需用户操作）LED 自动亮起
- [ ] 相同指令 5 分钟内不重复（日志出现 `cooldown, skip`）
- [ ] 静置 30s 后日志出现周期推理记录
- [ ] 推理 prompt 日志包含历史 belief 内容
