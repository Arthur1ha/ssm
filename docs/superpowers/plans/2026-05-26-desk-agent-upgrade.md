# DeskAgent 单智能体升级计划

> 生成时间：2026-05-26  
> 背景：毕设"在场式智慧空间"，当前阶段专注单智能体体验升级，暂不涉及三大核心交互（Capability Surfacing / Negotiated Execution / Memory-Driven Initiative）

---

## 一、本次升级目标

把当前"功能正确但体验平淡"的 DeskAgent，改造成一个**有性格、有表达、有记忆、感知更丰富**的桌面空间智能体。

核心理念：**让用户感觉空间是活的，不是一个自动化脚本**。

---

## 二、当前状态速览

| 文件 | 当前状态 |
|------|---------|
| `server/orchestrator/desk_agent.py` | 有 sense→reason→act 主循环，有短期 belief_history（10条，不持久化），无时间上下文，无语音输出 |
| `agents/phone/src/ui/AiLog.js` | 显示 rule_fired 和 evaluation，但用户无互动 |
| `agents/phone/src/app.jsx` | 有 MQTT 接收，无 TTS 逻辑 |
| `server/orchestrator/shared_state.py` | 存传感器/执行器/manifest，无语音 topic |
| `server/orchestrator/main.py` | MQTT 路由，可加新 topic 处理 |

---

## 三、功能清单

### F1. 手机喇叭 TTS（语音输出）

**方案**：云端生成文字 → MQTT 推送到 PWA → `window.speechSynthesis` 朗读

**新增 MQTT topic**：
```
ssm/agents/desk/speech   ← { text: "...", priority: "normal|urgent" }
```

**改动文件**：
- `agents/phone/src/app.jsx`：订阅 `ssm/agents/desk/speech`，收到后调用 `speechSynthesis.speak()`
- `server/orchestrator/desk_agent.py`：新增 `_speak(text)` 方法，发布到上述 topic
- `server/orchestrator/main.py`：确认该 topic 可发布（检查 MQTT publish 权限）

**注意**：
- iOS Safari 的 speechSynthesis 需要用户手势激活，首次加载 PWA 时加一个"启用语音"按钮
- 语速建议 `rate=0.9`，音调 `pitch=1.1`，中文声音选 `lang='zh-CN'`
- 防重播：同一文本 3 秒内不重复播放

---

### F2. 智能体性格设计（贱贱的，懂梗，嘴刁）

**改动文件**：`server/orchestrator/desk_agent.py`

在 `_reason()` 的 prompt 里加入人格定义，单独提取为常量：

```python
AGENT_PERSONA = """
你是一个桌面空间智能体，性格设定如下：
- 说话贱贱的，嘴比较刁，但不失礼貌
- 懂网络梗，偶尔用但不过度
- 对自己的判断有点自信，偶尔会吐槽环境或用户行为
- 执行动作时会带上一句评论，比如"行吧，给你开了"、"这光线也太暗了吧"
- 主动报告时语气轻松，不像机器人
- 说话简洁，不超过两句话
"""
```

**speech_text 字段**：在 LLM 输出 JSON 里加一个 `speech_text` 字段，内容是智能体"说出来"的话（区别于 `reason` 的内部日志）：

```json
{
  "context": "...",
  "space_mood": "...",
  "should_act": true,
  "action": {...},
  "reason": "内部日志，不播放",
  "speech_text": "这光也太拉了，给你开了啊，别谢我"
}
```

---

### F3. 多模态表达（LED + 语音同步）

**新增 MQTT topic**：
```
ssm/agents/desk/led_mood   ← { mood: "thinking|speaking|done|idle" }
```

**LED 行为映射**（在 ESP32 的 `bsm.py` 或 `trigger_map.py` 扩展）：

| mood | LED 行为 |
|------|---------|
| `thinking` | 慢速蓝色呼吸（frequency=0.5Hz） |
| `speaking` | 随机微闪（模拟说话节奏） |
| `done` | 短暂亮一下再回到当前状态 |
| `idle` | 恢复到 DeskAgent 决定的正常状态 |

**时序**：
```
DeskAgent 触发决策
  → 发布 led_mood=thinking
  → 生成 speech_text
  → 发布 led_mood=speaking
  → 发布 speech topic（PWA 开始朗读）
  → 发布执行指令
  → 发布 led_mood=done
  → 延迟 2s → 发布 led_mood=idle
```

**改动文件**：
- `server/orchestrator/desk_agent.py`：`_act()` 前后插入 mood 发布
- `agents/esp32/bsm.py`：新增 `LED_MOOD` 指令处理（接收 led_mood topic 并执行对应 LED 效果）
- `agents/esp32/trigger_map.py`：订阅 `ssm/agents/desk/led_mood`，路由到 BSM

---

### F4. 主动报告（Proactive Status Reporting）

**触发条件**（在 `_loop()` 里判断）：

1. 连续工作超过 60 分钟（靠 `_last_any_event_ts` 推算有人在场时长）
2. 传感器检测到环境发生显著变化（light_level 档位跳变）
3. 已执行某个动作超过 5 分钟，环境仍未改善

**实现**：在 `_reason()` 的 JSON 输出中增加 `proactive_report` 布尔字段，若为 true 则发布 `speech_text` 但不一定有 `action`。

示例报告：
- "你已经在这坐了一个多小时了，动一动？"
- "灯调完了，但感觉光线还是有点奇怪，你自己判断吧"
- "好久没声音了，你是睡了还是专注到忘我了？"

---

### F5. 思考过程外化（Verbalize Reasoning）

**改动**：`_reason()` 增加 `thought_text` 字段，记录智能体"觉得有意思"的推理过程，条件性播报：

```json
{
  "thought_text": "光线在往暗走，但声音传感器说有动静，这俩加一块就是有人在摸黑工作",
  "speech_text": "你在摸黑上班？给你开了"
}
```

**播报规则**：
- `thought_text` 在 DeskAgent 认为本次决策"有推理价值"时才发布（LLM 自行判断 `should_verbalize_thought: true/false`）
- 避免每次都播，只在有意思的情境下说

---

### F6. 传感器融合优化

**改动文件**：`server/orchestrator/desk_agent.py` → `_sense()`

**新增组合语义**，在返回的 sense_data 里加 `context_combo` 字段：

```python
# 在 _sense() 末尾推断组合语义
if level in ("DARK", "DIM") and sound_detected:
    combo = "dark_active"      # 黑暗中有人活动
elif level in ("DARK", "DIM") and not sound_detected:
    combo = "dark_silent"      # 可能离开或睡着
elif level in ("NORMAL", "BRIGHT") and sound_detected:
    combo = "normal_active"    # 正常工作状态
else:
    combo = "normal_silent"    # 安静正常

sense_data["context_combo"] = combo
```

在 `_reason()` 的 prompt 里用 `context_combo` 替代分散的字段描述，LLM 理解更准确。

---

### F7. 时间上下文

**改动文件**：`server/orchestrator/desk_agent.py` → `_sense()` 或 `_reason()`

在 sense_data 加入：

```python
import datetime
now_dt = datetime.datetime.now()
sense_data["time_str"] = now_dt.strftime("%H:%M")
sense_data["time_period"] = (
    "深夜" if 0 <= now_dt.hour < 6 else
    "清晨" if 6 <= now_dt.hour < 9 else
    "上午" if 9 <= now_dt.hour < 12 else
    "下午" if 12 <= now_dt.hour < 18 else
    "傍晚" if 18 <= now_dt.hour < 21 else
    "夜间"
)
```

在 prompt 里加一行："当前时段：{time_period}（{time_str}），请结合时段判断合理行为。"

---

### F8. 信念历史摘要

**改动文件**：`server/orchestrator/desk_agent.py`

**当前问题**：`_belief_history` 直接把最近3条原始条目拼入 prompt，token 消耗大且冗余。

**改进**：每积累 5 条信念后，调用一次 LLM 做摘要，存到 `_belief_summary`；`_reason()` 优先用摘要：

```python
self._belief_summary: str = ""   # 新增属性
self._beliefs_since_summary: int = 0  # 新增计数器

# 每5条做一次摘要
if self._beliefs_since_summary >= 5:
    self._belief_summary = self._summarize_beliefs()
    self._beliefs_since_summary = 0
```

`_summarize_beliefs()` 调用 LLM，prompt 类似："以下是最近的空间状态变化记录，用一句话总结规律：..."

---

## 四、建议实施顺序

```
Phase 1（体验感知最强，先做）
  F7 时间上下文      ← 改一行代码，收益大
  F6 传感器融合      ← 改 _sense()，逻辑更清晰
  F2 性格设计        ← 改 prompt，立刻有趣

Phase 2（加语音，体验质变）
  F1 手机 TTS        ← PWA + DeskAgent 各改一处
  F4 主动报告        ← 依赖 F1，F2
  F5 思考过程外化    ← 依赖 F1，F2

Phase 3（多模态，最复杂）
  F8 信念历史摘要    ← 依赖 F2 的 JSON 格式稳定后做
  F3 LED + 语音同步  ← 依赖 F1，需改 ESP32 代码
```

---

## 五、新 MQTT Topic 汇总

| Topic | 方向 | Payload |
|-------|------|---------|
| `ssm/agents/desk/speech` | 云→PWA | `{ text, priority }` |
| `ssm/agents/desk/thought` | 云→PWA（可选展示） | `{ text }` |
| `ssm/agents/desk/led_mood` | 云→ESP32 | `{ mood: thinking/speaking/done/idle }` |

---

## 六、关键约束提醒

- ESP32 架构：BSM 只做硬件，不调 MQTT；TriggerMap 是唯一接线点
- 所有新 MQTT publish 走 `_publish_task` 或专用 publish 函数，不在 `_reason()` 里直接发
- 性格 prompt 单独提取为模块级常量 `AGENT_PERSONA`，不要内联在 prompt 字符串里
- iOS speechSynthesis 需用户手势激活，PWA 需加"启用语音"按钮

---

## 七、验证方法

每个 Feature 独立验证：
- F1：PWA 收到 `ssm/agents/desk/speech` 消息后手机出声
- F2：DeskAgent 日志里 `speech_text` 有性格
- F3：手机开发者工具 MQTT 能看到 `led_mood` topic 消息
- F6：`sense` 日志里有 `context_combo` 字段
- F7：`sense` 日志里有 `time_period` 字段
- F8：每5次 belief 后 `_belief_summary` 非空
