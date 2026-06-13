# SSM 标识符词典（单一真实来源）

本系统**只有两个 ID 概念**。任何代码/文档新增标识符前，先在此登记。

| 概念 | 唯一字段名 | 含义 | 示例 | 谁用 |
|------|-----------|------|------|------|
| 整机 / 连接 | `device_id` | 一块物理板子 / 一个进程；单单元设备里 == unit_id | `esp32_desk`、`go2` | LWT/status、rules、location、manifest.parent_id |
| 传输单元 | `unit_id` | 每个可寻址智能体的唯一 ID，`{device_id}_{unit}` | `esp32_desk_led`、`go2` | **所有 topic、所有 payload 自标识、CardRegistry key、URL、Planner 对 LLM 的索引** |

> **slug 已废弃**：曾作展示别名，但与 unit_id 几乎重合（仅 `desk-lamp` 一例）、无实际需求，已合并进 unit_id。寻址/URL 用 `unit_id`，显示用 `name`，分类用 `tags`，slug 不再存在。

## 非 ID 属性（不参与寻址，别拿去拼 topic / 当 key）

| 字段 | 职责 | 唯一? | 例子 |
|------|------|-------|------|
| `name` | 给人看的显示标签 | ❌ | `桌面灯`、`Go2 机器狗` |
| `tags` / `agent_type` | 分类（决定图标/逻辑） | ❌ | `lighting` / `actuator` |

## 硬性规则

1. **构造任何 topic、任何 payload 自标识，一律用 `unit_id`。** 禁止再用 `agent`、`agent_id`、`slug`、`device_id` 当 payload 键。
2. `device_id` 仅用于「整机级」topic：`ssm/agents/{device_id}/status`、`ssm/rules/{device_id}`、`ssm/sys/pong/{device_id}`、`ssm/agents/{device_id}/location`。
3. `parent_id` 是 manifest/card 里指向 `device_id` 的字段名，专用于「子单元继承父设备在线/位置」。
4. 能力标签字段统一叫 `tags`，禁止 `agent_tag` / `resource_tags`。
5. 显示名一律放 `name`，禁止靠 `name` 子串反推设备类型（用 `tags`）。

## 收敛历史

- ✅ slug 已删除，合并进 unit_id。
- ✅ ESP32 payload 自标识键已由 `agent` 改为 `unit_id`。
- ✅ `config.py` 常量已由 `AGENT_*` 改为 `UNIT_*` / `DEVICE_ID`。
