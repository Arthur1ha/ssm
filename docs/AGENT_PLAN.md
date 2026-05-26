# SSM 智能体化升级计划

> 目标：将当前"哑终端 + 中心编排器"架构，逐步演进为真正的**分布式多智能体系统**。
> 分三个阶段推进，每阶段可独立运行和验证。

---

## 背景与核心判断

**当前系统的本质问题**：ESP32 是哑终端（感知/执行工具），云端 LangGraph 是唯一大脑，四个节点（Planner/Dispatcher/Evaluator/Responder）跑在同一进程、共享同一状态——这是微服务流水线，不是多智能体系统。

**目标架构**：每个物理设备对应一个完整的"感知-推理-行动"智能体。ESP32 是智能体的**身体**（传感器 + 执行器），云端专属推理进程是智能体的**心智**（LLM 推理循环）。后续换 ESP32-S3 + ESP-Claw 后，心智迁移到设备本地。

---

## 第一阶段：单智能体核心循环

### 目标

在云端构建一个完整的**感知→推理→执行**内部循环，使用已有的传感器数据（光线 + 声音）和执行能力（LED），不涉及任何新的对外通信设计。

```
[ESP32 已有数据流，透明管道]
  光线传感器 ──▶ shared_state
  声音传感器 ──▶ shared_state

[本阶段新建]
  DeskAgent
    ├── 感知：读 shared_state 中的传感器数据
    ├── 推理：LLM 解读情境 → 决策
    └── 执行：调用现有 do_publish_task 控制 LED

[验证方式]
  服务端日志 + 观察灯泡实际行为
```

MQTT 和 PWA 在本阶段是透明的——传感器数据已经进来，LED 控制已经有接口，不新增任何 topic 或通信协议。

---

### 任务 1：DeskAgent 类骨架

**目标**：建好空壳，能启动，不崩溃。

- 新建 `server/orchestrator/desk_agent.py`
- 定义 `DeskAgent` 类，构造函数接收 `shared_state`、`publish_task_fn`、`llm`
- 定义内部 belief state 结构（纯 Python dict，不对外暴露）：
  ```python
  belief = {
      "context": "",      # 对当前空间的一句话描述
      "space_mood": "",   # 专注 / 空闲 / 嘈杂 / 昏暗
      "should_act": False,
      "action": {},       # {"device": ..., "cmd": ..., "params": ...}
      "ts": 0.0,
  }
  ```
- 暴露 `start()` 方法，内部用 `threading.Thread` 运行独立循环（不动现有 asyncio 结构）
- 在 `main.py` 初始化段调用 `desk_agent.start()`

**验证**：启动服务后日志出现 `[DeskAgent] running`，现有功能（用户指令、规则引擎）不受影响。

---

### 任务 2：感知——读取传感器快照

**目标**：智能体能"看到"当前空间状态。

- 实现 `_sense()` 方法，调用 `shared_state.sensor_snapshot()`
- 从快照中提取关键字段，构造简洁的感知摘要：
  ```python
  {
      "light_level": "DIM",   # DARK / DIM / NORMAL / BRIGHT
      "light_lux": 180,
      "sound_detected": False,
      "sound_recent": False,  # 5s 内是否有声音事件
  }
  ```
- 处理数据缺失情况（传感器离线时返回 None，跳过本次推理）

**验证**：单独调用 `_sense()` 打印结果，与 ESP32 实际状态一致。

---

### 任务 3：推理——LLM 解读情境与决策

**目标**：给定传感器摘要，输出对空间的理解和行动决策。

- 实现 `_reason(sense_data)` 方法
- 构建 prompt，要求 LLM 输出固定格式 JSON：
  ```
  你是一个桌面空间智能体。根据传感器数据判断当前情境，并决定是否需要调节照明。

  传感器数据：{sense_data}
  上一次判断：{last_belief["context"]}（若无则忽略）

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
- 用 `json.loads` 解析，解析失败时保留上一次 belief，打印警告

**验证**：
- 模拟 `light_level=DARK` 的 sense_data 直接调用 `_reason()`
- 日志打印推理结果，`should_act=True`，`action` 为开灯

---

### 任务 4：执行——自主控制 LED

**目标**：推理结果为"需要行动"时，智能体自主发出控制指令。

- 实现 `_act(belief)` 方法
- 从 `belief["action"]` 取出 device / cmd / params
- 调用已有的 `do_publish_task(device_id, task_id, action, params, session_id="agent_auto")`
- 行动冷却：同一 cmd+params 组合在 **5 分钟**内不重复触发（防止反复开关）
  ```python
  _cooldown: dict[str, float] = {}  # key=f"{cmd}_{params}", value=last_trigger_ts
  ```

**验证**：
- 遮住光线传感器 → 等待智能体推理 → LED 自动亮起
- 再次遮住 → 冷却期内不重复触发（日志显示 `[DeskAgent] cooldown, skip`）

---

### 任务 5：循环驱动——事件触发 + 周期 tick

**目标**：让智能体持续运转，不是一次性的。

- 实现主循环 `_loop()`，两种触发方式：

  **方式 A：事件触发**
  - `main.py` 在 `trigger="sensor"` 时，额外向 DeskAgent 的内部队列（`queue.Queue`）放一条事件
  - DeskAgent 从队列取事件，立即执行 sense→reason→act

  **方式 B：周期 tick**
  - 队列等待超时（30s）后自动触发一次推理
  - 保持智能体"持续在场"，不依赖传感器变化

- 事件去抖：同一传感器在 **5s** 内的多次事件合并为一次推理

**验证**：
- 拍手（声音事件）→ 10s 内看到推理日志
- 静置 30s → 自动触发周期推理日志

---

### 任务 6：短期记忆——belief 历史

**目标**：让推理能感知趋势，不只看当前快照。

- 维护 `_belief_history: list[dict]`，最多保留最近 **10 条**（内存，不写文件）
- 每次推理后 append 最新 belief
- 在 `_reason()` 的 prompt 中附加最近 3 条 belief 的 context，让 LLM 感知变化趋势：
  ```
  近期状态变化：
  - 2分钟前：空间空闲，光线正常
  - 1分钟前：有人进入，光线开始变暗
  - 刚才：光线偏暗，有人在场
  ```

**验证**：
- 观察推理 prompt（打印到日志）中包含历史记录
- 相同传感器数据下，有历史上下文的推理结果与无历史时不同（更准确）

---

### 本阶段完成标准

- [ ] DeskAgent 独立线程运行，不影响现有用户指令和规则引擎
- [ ] 遮住光线传感器后，无需任何用户操作，LED 自动亮起
- [ ] 冷却机制有效：5 分钟内相同指令不重复发出
- [ ] 30s 无事件后自动触发一次推理（日志可见）
- [ ] 推理 prompt 包含最近 3 条历史 belief

---

### 本阶段完成后扩展（不在本阶段做）

1. **对外发布**：belief state / intent 发布到 MQTT，供其他智能体和 PWA 订阅
2. **持久化记忆**：memory.json，重启后加载历史
3. **PWA 联动**：belief 展示、协商执行、主动消息
4. **多智能体**：拆出 PerceptionAgent / MemoryAgent / InitiativeAgent

---

## 第二阶段：多智能体系统（多个独立云端智能体）

> 前提：第一阶段完成且稳定运行

### 目标

把单一 DeskAgent 拆解为多个职责独立、通过 MQTT 协商的自主智能体，实现真正的分布式 MAS。

### 智能体划分

| 智能体 | 职责 | 运行方式 |
|--------|------|---------|
| `PerceptionAgent` | 持续解读所有传感器 → 维护空间状态模型 | 事件驱动，实时 |
| `MemoryAgent` | 跨 session 记忆管理，响应查询 | 被动监听 + 响应 |
| `InitiativeAgent` | 监测状态变化 → 决定是否主动开口 | 监听 PerceptionAgent 输出 |
| `IntentAgent` | 处理用户显式请求（现有编排器重构） | 用户触发 |

### 通信协议（智能体间 MQTT）

```
PerceptionAgent → ssm/space/state       空间状态快照（每次更新）
MemoryAgent     → ssm/space/memory/res  查询结果响应
InitiativeAgent → ssm/initiative/+      主动消息
IntentAgent     → ssm/feedback/+        执行反馈
```

### 实现步骤

1. 从 DeskAgent 中拆出 PerceptionAgent（独立进程/asyncio task）
2. 将 memory.py 升级为 MemoryAgent（支持 MQTT 查询接口）
3. 新建 InitiativeAgent，监听 `ssm/space/state`，用 LLM 判断是否需要主动发言
4. 重构 IntentAgent（现有编排器），改为从 PerceptionAgent 拉取实时空间状态
5. 各智能体启动时发布自身 manifest（当前只有 ESP32 和 pc_decision 有 manifest）

---

## 第三阶段：硬件升级（ESP32-S3 + ESP-Claw）

> 前提：第二阶段完成，或第一阶段验证用户研究后进行

### 目标

将智能体的推理从云端迁移到边缘设备，实现真正的**分布式边缘智能体**。

### 硬件选型

- 芯片：ESP32-S3（双核 LX7，AI 向量指令）
- 内存：≥ 8MB PSRAM + 8MB Flash
- 推荐开发板：任意带 8MB PSRAM 的 ESP32-S3 模组（如 ESP32-S3-DevKitC-1）
- 估价：30–60 元/块

### 迁移步骤

1. 购买 ESP32-S3 开发板，验证硬件引脚兼容性（GPIO 编号可能变化）
2. 用 ESP-Claw 框架重写边缘侧代码（C/Lua，替换 MicroPython）
3. 将第一阶段 DeskAgent 的推理逻辑迁移为 ESP-Claw 的 Lua agent 脚本
4. 云端 DeskAgent 降级为"协调层"（不再做设备级推理，只做跨设备协调）
5. 验证离线能力：断开云端后，设备凭 Lua 脚本继续自主运行

### 论文叙事

第三阶段完成后，系统可以诚实地描述为：
> "边缘设备运行本地智能体循环（ESP-Claw），云端提供跨设备协调与记忆服务。推理分布在边缘和云端，具备断网自治能力。"

---

## 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 推理位置（阶段1/2） | 云端专属进程 | 当前 ESP32 无 PSRAM，无法运行智能体运行时 |
| 推理位置（阶段3） | 边缘设备本地 | ESP32-S3 + ESP-Claw 支持本地 agent loop |
| 智能体通信协议 | MQTT（与现有架构一致） | 天然解耦，ESP32/PWA/云端三层共用同一总线 |
| 记忆存储（阶段1） | 服务器本地 JSON 文件 | 简单可靠，无需引入数据库 |
| 记忆存储（阶段3） | 设备本地（ESP-Claw 结构化记忆） | 隐私保护，离线可用 |
| 协商执行默认行为 | 所有 LLM 规划的任务均需确认 | 保证用户掌控感（论文 RQ2 核心变量） |

---

## 与论文的对应关系

| 论文交互形式 | 对应实现 | 所在阶段 |
|------------|---------|---------|
| Capability Surfacing（能力呈现） | DeskAgent 生成能力描述 + PWA 弹窗展示 | 阶段1 步骤4 |
| Negotiated Execution（协商执行） | plan_preview + 用户确认流程 | 阶段1 步骤3 |
| Memory-Driven Initiative（记忆建议） | memory.py + Planner 注入历史 | 阶段1 步骤2 |
| Proactive Initiative（主动发声） | DeskAgent 状态变化触发主动消息 | 阶段1 步骤1 |
| 真正分布式 MAS | 多智能体协商 + 边缘智能体 | 阶段2/3 |
