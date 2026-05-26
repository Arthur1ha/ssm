# SSM Architecture V2 — 三层智能体编排 + 渐进式反馈

## 核心设计原则

用户只感知**需求是否被理解**和**结果是否符合预期**，不感知任何硬件细节。
每一个智能体层完成自己的工作后**立刻向用户反馈**，而不是等整条链路走完再说话。

---

## 系统分层

```
┌─────────────────────────────────────────────┐
│           手机端 NLU Agent                   │  层一：理解意图
│   自然语言 → 结构化意图 → 立刻反馈用户        │
└────────────────────┬────────────────────────┘
                     │ MQTT: ssm/intent/{session_id}
                     ▼
┌─────────────────────────────────────────────┐
│         云端 Orchestrator（总控）             │  层二：规划 + 分发
│   查能力 → 生成计划 → 下发任务 → 反馈进度     │
└──────────┬──────────────────────────────────┘
           │ MQTT: ssm/task/{device_id}/{task_id}
           ▼
┌─────────────────────────────────────────────┐
│          ESP32 Edge Agent（端侧）             │  层三：执行
│   接收任务 → ISM 验证 → BSM 执行 → 上报结果  │
└─────────────────────────────────────────────┘
```

---

## 完整链路示例

**用户输入：** "感觉有点暗，我想看书"

```
① 手机 NLU Agent 解析（<1s）
   → 立刻反馈用户："明白了，你想看书，我来帮你调亮一点。"
   → 发送结构化意图到云端

② 云端 Orchestrator 规划（1-2s）
   → 立刻反馈用户："正在调整灯光..."
   → 查询能力注册表，找到 LED 设备
   → 生成任务并下发给 ESP32

③ ESP32 执行（<1s）
   → 执行灯光调整
   → 上报执行结果给云端

④ 云端收到结果，反馈用户
   成功："好了，灯已经调亮，适合阅读了。"
   部分成功："灯已调到最亮，如果还不够，可能需要台灯补光。"
   失败："灯没有响应，可能连接有问题，你可以手动试试。"
```

---

## 各阶段反馈规范

### 反馈原则
- 语言层次 = 用户需求层次，**不暴露技术细节**
- 每层完成立刻推送，不等整条链路结束
- 承认限制时主动提供替代方案，不让对话死掉

### 各阶段反馈内容

| 阶段 | 触发时机 | 反馈示例 | 不要说 |
|------|---------|---------|--------|
| NLU 解析完成 | 意图识别后立刻 | "明白了，你想看书，我来调亮灯光。" | "已解析 intent=reading_mode" |
| 云端规划中 | 下发任务前 | "正在调整灯光..." | "正在查询 capability registry" |
| 执行成功 | ESP32 上报 ok | "好了，灯已经调亮，适合阅读了。" | "esp32_led 执行成功，brightness=200" |
| 能力不足 | 设备无法满足 | "灯已调到最亮了，如果还是觉得暗，可能需要开台灯。" | "设备不支持该参数范围" |
| 完全不支持 | 无对应设备 | "抱歉，我现在只能控制灯光，没有音响设备。要不先把灯调成适合放松的暖色？" | "未找到匹配 resource_tag=audio 的设备" |
| 执行失败 | 设备无响应 | "灯没有响应，可能连接有问题，你可以手动试试。" | "MQTT timeout，task_id=xxx" |

---

## MQTT Topic 协议

### 新增 Topic（替换现有 command）

| Topic | 方向 | Payload | 说明 |
|-------|------|---------|------|
| `ssm/intent/{session_id}` | 手机 → 云端 | 见下 | 用户意图 |
| `ssm/feedback/{session_id}` | 云端 → 手机 | 见下 | 各阶段反馈 |
| `ssm/task/{device_id}/{task_id}` | 云端 → ESP32 | 见下 | 具体执行任务 |
| `ssm/result/{device_id}/{task_id}` | ESP32 → 云端 | 见下 | 执行结果 |

### Payload 格式

**ssm/intent/{session_id}**
```json
{
  "session_id": "s_20260427_001",
  "user_msg": "感觉有点暗，我想看书",
  "requirements": [
    { "resource_tag": "lighting", "action": "brighten", "context": "reading" }
  ],
  "priority": 8,
  "ts": 1714200000
}
```

**ssm/feedback/{session_id}**
```json
{
  "session_id": "s_20260427_001",
  "stage": "nlu_done",
  "text": "明白了，你想看书，我来帮你调亮一点。",
  "status": "ok",
  "ts": 1714200001
}
```
`stage` 枚举值：`nlu_done` | `planning` | `executing` | `done` | `partial` | `failed`

**ssm/task/{device_id}/{task_id}**
```json
{
  "task_id": "t_001",
  "session_id": "s_20260427_001",
  "action": "SET_COLOR",
  "params": { "r": 255, "g": 220, "b": 150, "brightness": 200 },
  "ts": 1714200002
}
```

**ssm/result/{device_id}/{task_id}**
```json
{
  "task_id": "t_001",
  "session_id": "s_20260427_001",
  "result": "ok",
  "ism_state": "ON",
  "ts": 1714200003
}
```
`result` 枚举值：`ok` | `blocked` | `timeout` | `error`

---

## 云端 Orchestrator 内部结构（LangGraph）

```
[START]
   ↓
[Planner Node]        查能力注册表，生成任务列表，推送 "planning" 反馈
   ↓
[Dispatcher Node]     逐任务下发，推送 "executing" 反馈
   ↓
[Evaluator Node]      收集所有 result，评估整体成功率
   ↓
[Responder Node]      生成自然语言反馈，推送 "done/partial/failed"
   ↓
[END]
```

---

## 设备能力注册表（Capability Manifest 扩展）

在现有 `agent_manifest.publish()` 中增加 `capabilities` 字段：

```json
{
  "unit_id": "esp32_led",
  "capabilities": [
    { "action": "SET_COLOR",   "params": ["r", "g", "b", "brightness"] },
    { "action": "DIM",         "params": ["level"], "range": [0, 100] },
    { "action": "BLINK",       "params": ["count"] },
    { "action": "SET_STATE",   "params": ["state"], "values": ["ON", "OFF"] }
  ],
  "resource_tags": ["lighting", "ambiance"]
}
```

Orchestrator 维护 `{ resource_tag → [device_id] }` 索引，收到意图后自动路由。

---

## 硬件规划

| 状态 | 设备 | 接口 | 说明 |
|------|------|------|------|
| 已有 | RGB LED（单颗）| GPIO 4/16/17 | 当前开发用 |
| 待购 | WS2812B 灯带（1m）| 单线数据，1 个 GPIO | 支持逐像素控制，替换 RGB LED |

WS2812B 接入后，BSM 层需增加逐像素控制逻辑（NeoPixel 协议）。

---

## 实施顺序

### Step 1 — 协议落地
- [ ] 按本文档定义所有 topic 的 payload schema
- [ ] 在 broker 上验证 topic 收发（用 MQTT Explorer 测试）

### Step 2 — 云端 Orchestrator 改造
- [ ] `pc_agent/graph.py`：拆分为 Planner → Dispatcher → Evaluator → Responder 四节点
- [ ] 增加能力注册表（内存 dict，从 manifest 消息自动构建）
- [ ] 每节点完成后发布对应 stage 的 `ssm/feedback/{session_id}`

### Step 3 — 手机 NLU Agent
- [ ] PWA 增加自然语言输入框
- [ ] 调用 LLM 解析意图，生成 requirements[]
- [ ] 发送 `ssm/intent/{session_id}`
- [ ] 订阅 `ssm/feedback/{session_id}`，将 `text` 字段展示给用户

### Step 4 — ESP32 端侧升级
- [ ] `trigger_map.py`：增加 `ssm/task/{device_id}/+` 订阅
- [ ] 执行完成后发布 `ssm/result/{device_id}/{task_id}`
- [ ] `agent_manifest.py`：补充 `capabilities` 字段

### Step 5 — WS2812B 灯带接入
- [ ] 购入硬件后，`bsm.py` 增加 NeoPixel 控制逻辑
- [ ] 新增 `DIM`、逐像素渐变等 action
- [ ] 更新 manifest capabilities

---

## 暂不实现

- 音频 / 蜂鸣器的智能控制
- 多用户冲突仲裁
- 上下文记忆（个性化历史）
- 离线降级 / Fallback 链（主链路稳定后再加）
