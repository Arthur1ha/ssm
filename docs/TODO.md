# SSM 待办事项

> 按优先级排列，已完成移至底部存档。

---

## 待办

### 高优先级

- [ ] **规则本地执行反馈上报**
  ESP32 本地规则触发时，发布 `ssm/agents/{id}/report`（携带 `source: "local_rule"` 字段），让云端能感知离线期间实际发生的操作。

- [ ] **清理 V1 decision_node 死代码**
  `graph.py` 中 `decision_react` 和 `decision_node` 已不再被调用（sensor 事件不再进入 `graph.invoke()`），可安全删除，只保留 `evaluation_react`。

### 中优先级

- [ ] **规则组合条件（AND）**
  支持多条件组合，如"光线暗 AND 有人 → 开灯"、"无人 → 关灯"。
  设计方向：`trig` 改为列表 `[{tag, ev}, ...]`，ESP32 端 `_match()` 全部满足才触发。

- [ ] **PWA 规则页直接编辑**
  目前规则只能通过 AI 对话创建，考虑在规则页增加表单化创建入口（选择触发条件和动作），降低上手门槛。

### 低优先级

- [ ] **多设备规则路由**
  `_push_rules_to_esp32()` 目前硬编码 `_ESP32_AGENTS = ["esp32_desk"]`，后续接入第二台设备时需要动态查能力注册表来决定推送目标。

---

## 已完成

- [x] **规则引擎**：云端 `rule_engine.py`，零 LLM，传感器事件触发，热加载 `rules.json`
- [x] **规则 CRUD API**：`GET/POST/DELETE/PATCH /api/rules`
- [x] **NLU 规则定义**：`/api/nlu` 识别 `define_rule` 意图，PWA 预览确认后保存
- [x] **规则云端→本地同步**：CRUD 后推送 `ssm/rules/{agent_id}`（retain），ESP32 flash 缓存
- [x] **ESP32 动态规则**：`local_rules.py` 删除全部硬编码，改为执行云端同步的规则
- [x] **sensor 事件不再触发 LLM**：修复声音传感器高频触发导致 intent 队列阻塞、手机 30s 超时的 bug
- [x] **架构文档更新**：`docs/ARCHITECTURE.md` 同步三条决策路径、规则格式、新 topic
