# DeskAgent Phase 1 设计文档

**日期**：2026-05-21  
**阶段**：第一阶段——单智能体核心循环  
**参考**：`docs/AGENT_PLAN.md`

---

## 目标

在云端构建一个完整的 **感知 → 推理 → 执行** 内部循环，使用已有传感器数据（光线 + 声音）和 LED 控制能力，实现无需用户指令的自主空间感知与照明调节。

---

## 架构

### 新增文件

| 文件 | 描述 |
|------|------|
| `server/orchestrator/desk_agent.py` | DeskAgent 类，独立线程运行 |

### 修改文件

| 文件 | 改动量 | 说明 |
|------|--------|------|
| `server/orchestrator/main.py` | +4 行 | 初始化 DeskAgent，传感器事件转发 |

---

## DeskAgent 类设计

### 构造函数

```python
DeskAgent(shared_state, publish_task_fn, llm)
```

- `shared_state`：`SharedState` 实例，用于读取传感器快照
- `publish_task_fn`：`tools.do_publish_task`，用于发送设备指令
- `llm`：`ChatOpenAI` 实例（通过同一套环境变量构建，不从 graph.py 导入）

### 公开方法

```python
start()
    # 启动后台 threading.Thread，不阻塞主线程

push_sensor_event(unit_id: str, payload: dict)
    # main.py 在 trigger="sensor" 时调用
    # 将事件放入内部 queue.Queue
```

### 内部状态

```python
belief = {
    "context":    "",      # 当前空间一句话描述
    "space_mood": "",      # 专注 / 空闲 / 嘈杂 / 昏暗
    "should_act": False,
    "action":     {},      # {"device": ..., "cmd": ..., "params": ...}
    "ts":         0.0,
}
_belief_history: list[dict]  # 最多 10 条，不写文件
_cooldown: dict[str, float]  # key="{cmd}_{params}", value=last_trigger_ts
_event_queue: queue.Queue
```

---

## 数据流

```
传感器事件 (main.py)
  ↓ push_sensor_event()
  queue.Queue
  ↓ _loop() — 去抖 5s，合并同传感器重复事件
  _sense()  — 从 shared_state.sensor_snapshot() 提取关键字段
  ↓
  {
    "light_level":    "DARK|DIM|NORMAL|BRIGHT",
    "light_lux":      int,
    "sound_detected": bool,
    "sound_recent":   bool,   # 5s 内是否有声音事件
  }
  ↓
  _reason(sense_data)  — LLM 推理，输出结构化 belief
  ↓
  _act(belief)         — 若 should_act=True 且未在冷却期，发送指令
  ↓
  do_publish_task(device_id, task_id, action, params, session_id="agent_auto")
```

### 周期触发

- `queue.Queue.get(timeout=30)` 超时 → 自动触发一次推理（不依赖传感器变化）

---

## LLM Prompt

```
你是一个桌面空间智能体。根据传感器数据判断当前情境，并决定是否需要调节照明。

传感器数据：{sense_data}
上一次判断：{last_belief["context"]}（若无则忽略）

近期状态变化（最近3条）：
- {history[-3]}
- {history[-2]}
- {history[-1]}

输出 JSON（不含代码块）：
{
  "context": "一句话描述当前情境",
  "space_mood": "专注/空闲/嘈杂/昏暗 中的一个",
  "should_act": true/false,
  "action": {
    "device": "esp32_desk_led",
    "cmd": "SET_STATE",
    "params": {"state": "BRIGHT"}
  },
  "reason": "为什么这样决定"
}
若 should_act 为 false，action 填 {}。
```

- 解析失败时保留上一次 belief，打印警告
- `json.loads()` 解析，提取第一个 `{...}` 块

---

## 冷却机制

```python
key = f"{cmd}_{json.dumps(params, sort_keys=True)}"
cooldown_secs = 300  # 5 分钟

if time.time() - _cooldown.get(key, 0) < cooldown_secs:
    print("[DeskAgent] cooldown, skip")
    return
_cooldown[key] = time.time()
```

---

## 短期记忆

- `_belief_history` 最多保留 10 条（内存，重启清零）
- 每次推理完成后 append 当前 belief
- prompt 中注入最近 3 条的 `context` + `ts` 字段

---

## main.py 改动

```python
# ── 初始化段（rule_engine 初始化之后）────────────────────
from desk_agent import DeskAgent
desk_agent = DeskAgent(state, agent_tools.do_publish_task, None)
desk_agent.start()

# ── sensor 分支（rule_engine.match_and_fire 之后）─────────
desk_agent.push_sensor_event(unit_id, event["payload"])
```

`llm=None` 时 DeskAgent 内部自行构建 LLM 实例。

---

## 与现有组件的边界

| 组件 | 关系 |
|------|------|
| RuleEngine | 并行共存，互不干预。两者均收到传感器事件，独立处理。 |
| 用户意图编排器 | 无交集。DeskAgent 不处理 `trigger="intent"` 事件。 |
| SharedState | 只读（`sensor_snapshot()`），不写入。 |
| tools.py | 调用 `do_publish_task()`，`session_id="agent_auto"`。 |

---

## 验证标准

- [ ] 服务启动后日志出现 `[DeskAgent] running`，现有功能不受影响
- [ ] 遮住光线传感器 → 无需用户操作 → LED 自动亮起
- [ ] 5 分钟内相同指令不重复发出（日志 `[DeskAgent] cooldown, skip`）
- [ ] 静置 30s → 自动触发周期推理（日志可见）
- [ ] 推理 prompt 包含最近 3 条历史 belief
