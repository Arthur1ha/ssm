# SSM 三智能体迁移至 LangChain / LangGraph 计划

> 日期：2026-10-31  
> 状态：代码迁移已在当前工作区实施，实机与 PWA 发布验收待完成；结果见 `docs/architecture/agent-langgraph-migration-comparison.md`  
> 范围：管家 `cloud_orchestrator`、智能灯 `esp32_desk_led`、机器狗 `go2` 的对话、委派、自主决策、记忆、人格回复和用户可见进度  
> 最高约束：保留现有人格与特质、三者的全部实际能力、API/设备协议，以及 PWA 当前展示内容和交互语义；继续使用 JSON/Markdown 轻量文件持久化，不引入后端数据库；三个智能体的输入、规划、执行、结果和回复均有可追踪日志；整理成清晰的多智能体目录并交付迁移前后对比报告

## 1. 目标和迁移边界

三个云端智能体统一使用 LangChain 的模型、工具和 Agent 组件，以及 LangGraph 的状态、循环、检查点、暂停恢复和流式运行能力。停止维护 SSM 自写的通用 Agent 循环、运行记录、工具调用 JSON 解析、澄清恢复和进度调度。设备专属的 MQTT/HTTP 传输、鉴权、Card 发现、FSM、运动安全和真实回执判定仍是 SSM 领域逻辑，以 LangChain 工具和 LangGraph 节点接入。ESP32 的 MicroPython BSM/ISM/TriggerMap 与云端离线本地规则属于固件控制层，不能在 ESP32 上运行 Python LangGraph，保持原有契约。

**旧 `graph.py` 的一次性批量规划不是目标架构。** 迁移结果是三个角色各有自己的 Agent 定义和人格、共用框架能力与约定、各自调用现有设备工具；不恢复旧图，不保留第二套 Agent 入口，也不把三个智能体合成一个假角色。

本次以**管理员现有体验与交互完整保留**为验收重点。暂不设计管理员/访客两套严格区分的 Agent 提示词、记忆产品体验或验收矩阵；现有 API 鉴权、会话隔离和同伴不能冒充用户的基本边界继续有效，不借迁移扩大权限。

框架选型依据：[LangChain `create_agent` 的工具循环和运行时上下文](https://docs.langchain.com/oss/python/langchain/agents)、[LangGraph 状态与持久化](https://docs.langchain.com/oss/python/langgraph/overview)、[暂停恢复](https://docs.langchain.com/oss/python/langgraph/interrupts)、[流式事件](https://docs.langchain.com/oss/python/langgraph/streaming)。`create_agent` 本身运行在 LangGraph 上；需要显式设备回执、暂停或安全路由时可用 `StateGraph`、`ToolNode` 组合确定性节点，但不得重新写 Python `for/while` Agent 循环。[官方工作流与 ToolNode 说明](https://docs.langchain.com/oss/python/langgraph/workflows-agents)

## 2. 现状盘点与不能丢的行为

| 路径 | 当前入口和能力 | 迁移后必须保持 |
| --- | --- | --- |
| 主聊天/管家 | `cloud/api/intents.py` → MQTT `ssm/intent/{session_id}` → `cloud/orchestrator/main.py` → `multitask.py`；按 Card 发现设备、读状态、跨 MQTT/HTTP 委派、保存待确认规则、启动活动、空间约束、同伴消息 | 管家以沉稳、有经验的口吻统筹；只报告已确认结果；主聊天进度、同伴气泡、追问续接、规则确认与设备发现仍显示在原位置 |
| 灯设备页/语义委派 | `cloud/esp32/router.py` → `agent.py.run_user_loop` → `multitask.py`；灯开关、RGB/亮度、闪烁、同伴技能、空间上下文、任务查询与回执 | 灯高傲、刁钻但不羞辱用户；不向用户堆亮度/RGB 数值；只在 ESP32 `result` 证实后说完成；`manual/reactive`、让位窗口和物理控件不变 |
| 灯自主与旁白 | `agentcore/mode/reactive.py`、`router.py` 的实体任务回复、活动内主动交谈 | 传感器触发时机、冷却、用户让位、同伴让位、独立于对话的活动发言、设备离线处理与回复口吻不变 |
| Go2 设备页/委派 | `cloud/go2/router.py` → `multitask.py`；姿态、定时运动、导航、视觉、身份、位置、规则、同伴请求、任务查询/取消、`/chat/stream` | Go2 可爱、活泼、童稚；控制租约、急停、FSM、地图/里程计约束、流式响应与连接状态不变；无法证实位移时不声称到达 |
| Go2 自主 | `agentcore/mode/reactive.py`、`free_explore.py`、视觉回调、活动内社交/探索 | `manual/reactive/free_explore` 现有启动/停止、好奇心探索机会、外向性社交机会、大胆程度候选、视觉触发、冷却和中断条件不变 |
| 跨智能体 | `cloud/peers/gateway.py`、`messaging.py`、各工具执行器；MQTT 签名、单跳和来源策略 | 独立智能体通过公开 Card 与受控入口协作；消息投递不等于接受，接受不等于物理完成；不得越权或递归委派 |
| 人格/记忆 | 三份 `agentcore/personality/{profile,behavior}.py`、`soul.py`、灯/Go2 事件记忆和每日摘要 | 三份已保存 `prompt/traits/revision` 原样保留；管理员编辑、记忆查看和持久化体验即时生效；Go2 演化、灯调光敏感度及三者回复差异仍能观察；现有会话隔离与记忆保留期不退化 |
| 前端 | `/api/intents`、`/api/events`、设备页聊天和 SSE；`useSendIntent.js`、`app.jsx`、`ActivityFeed.jsx`、`ChatPanel.jsx` | 页面结构、名称、气泡归属、过程小字、最终回复、活动流、人格设置、追问续接与去重行为不变；不出现新的框架术语或原始工具参数 |

以上以**当前运行代码与当前保存的人格资料**为基线，不以旧计划中的默认文案覆盖用户设置。对动态 LLM 文本，保持角色声音、事实含义和展示位置；对静态文案、事件字段和 UI 元素，做逐项回归比较。

## 3. 目标架构与职责归属

```text
PWA 现有 UI ← 原有 API/SSE 事件契约 ← Graph 事件投影适配器
                                           ↑
主聊天 MQTT → 管家 Agent ──受控工具──→ Card/MQTT 灯、HTTP Go2
灯页 HTTP   → 灯 Agent   ──受控工具──→ ESP32 MQTT result / 同伴网关
Go2 页 HTTP → Go2 Agent  ──受控工具──→ WebRTC/导航/视觉 / 同伴网关
传感器/视觉/活动触发 → 对应角色的 LangGraph 自主工作流
                 每个 Agent：LangChain 模型与工具 + LangGraph 检查点/流
                          ↓
                 共享 JSON/Markdown 文件存储 + 现有 session JSONL 日志
```

| 能力 | 框架承担 | SSM 保留的领域逻辑 |
| --- | --- | --- |
| 多轮模型→工具→结果→模型 | 优先 `create_agent`；需要确定性前后阶段时用 `StateGraph` 与 `ToolNode`，通过条件边循环 | 各角色可见工具、Card 最新状态和设备执行约束 |
| 参数与上下文 | `@tool`/Pydantic schema、`ToolRuntime`/运行时 `context_schema`、标准 `AIMessage`/`ToolMessage` | 用户会话鉴权、空间成员、来源、持久化许可、设备 FSM、运动租约 |
| 轮次/工具预算 | `ModelCallLimitMiddleware`、`ToolCallLimitMiddleware` 等官方组件；总截止时间作为运行时上下文与调用超时 | 设备动作有无效果、紧急停止和会话过期的业务判定。官方中间件能力见[文档](https://docs.langchain.com/oss/python/langchain/middleware/built-in) |
| 短期历史、澄清、恢复 | 持久化 checkpointer、每角色/会话 `thread_id`、`interrupt()` 与 `Command(resume=...)` | 原 `run_id`/`trace_id` 与 session 的可信映射；恢复前查询未决设备任务 |
| 长期情节记忆 | Graph 在节点/工具中按需读写角色记忆；若需 Store API，仅用文件后端适配该接口；短期会话由 checkpointer 管理 | 沿用各角色 JSON/JSONL 事件和 Markdown/现有摘要文件、鉴权、会话隔离、7 天保留、人物隐私及领域资产 |
| 人格与特质 | LangChain 动态系统提示词、结构化输出；Graph 在每次决策/回复读取最新资料 | 三份人设文案、分数到行为机会的映射、管理员设置 API 与 revision 并发控制；这是产品配置，不用模型自行修改 |
| 进度/可观测性 | Graph 流式运行事件和框架回调/trace 覆盖节点、模型、工具、检查点和恢复 | `cloud/observability/` 统一落盘；薄的 API/SSE 投影沿用现有事件名称、字段、隐私过滤和活动流归属；`make logs` 可查完整任务轨迹 |
| 外部动作的幂等 | Graph 检查点在工具节点前固定调用 ID；节点间保存状态 | 既有 ESP32/Go2 任务账本与设备回执；断点处先查询原 ID，不盲目重发 |

### 3.1 目标目录与命名约定

LangGraph 没有强制的文件命名标准；本项目采用清晰的 `graph.py`、`state.py`、`tools/` 等职责约定。以现有“每个智能体有独立 `agentcore`，设备/API 在外层”的划分为基础，把**全部云端 Agent 逻辑集中到一个 `cloud/agents/` 根目录**，不再让人格、Graph、记忆和工具分散在 `cloud/orchestrator`、`cloud/esp32`、`cloud/go2` 与 `cloud/agent_runtime` 四处。旧设备 ID、HTTP 路由和 MQTT topic 保持原名；这里只调整 Python 内部模块名和导入。

```text
cloud/
├── agents/
│   ├── common/                 # 框架装配：模型、运行时上下文、文件 checkpointer、事件投影
│   ├── steward/                # 管家；公开 ID 仍为 cloud_orchestrator
│   │   ├── graph.py            # 角色主对话 Graph 装配入口
│   │   ├── state.py            # LangGraph 类型化状态
│   │   ├── prompts.py          # 动态系统提示词与回复模板
│   │   ├── personality/        # 原 prompt、trait 及行为偏好
│   │   ├── memory/             # 仅放管家实际使用的文件记忆接线
│   │   ├── tools/              # 单次原子工具及参数 schema
│   │   └── skills/             # 对外能力与跨设备组合工作流
│   ├── lamp/                   # 智能灯；公开 ID 仍为 esp32_desk_led
│   │   ├── graph.py  state.py  prompts.py
│   │   ├── personality/  memory/  tools/  skills/
│   │   └── workflows/          # reactive、社交、实体任务旁白等独立 Graph 入口
│   └── go2/                    # 机器狗；公开 ID 仍为 go2
│       ├── graph.py  state.py  prompts.py
│       ├── personality/  memory/  tools/  skills/
│       └── workflows/          # reactive、explore、daily_summary、特质演化
├── orchestrator/               # MQTT worker、受信入口、Card/传输适配；无 Agent 决策代码
├── esp32/                      # API router、ESP32 状态/任务账本/MQTT 驱动；无 Agent 决策代码
├── go2/                        # API router、WebRTC、视觉采集、导航/运动与任务账本；无 Agent 决策代码
├── observability/              # 现有统一 JSONL 事件与日志配置；增加 Graph 事件采集
└── api/                        # 原 HTTP/SSE 契约和人格设置端点
```

目录职责固定：`graph.py` 只装配和运行角色主对话 LangGraph；`state.py` 只声明状态；`prompts.py` 只生成提示词；`personality/` 保存角色资料与特质映射；`memory/` 接现有文件记忆和摘要工作流所需的数据访问；`tools/` 包装**一次**设备或空间操作；`skills/` 声明对外能力或组合子图，不能再复制工具实现；`workflows/` 放非用户聊天触发的独立 Graph。`common/` 仅放共享框架配置及文件持久化适配器，日志写入继续归 `cloud/observability/`，避免新建第二套日志系统。没有实际职责的空目录不创建。Card 的公开 skill 与 Agent 可调用 tool 建立可核对的一对一/组合映射，不维护第二份互不相干的能力注册表。

| 现有文件/目录 | 目标位置或处理方式 |
| --- | --- |
| 三个 `multitask.py` | 拆入各自 `cloud/agents/{steward,lamp,go2}/graph.py`、`state.py`、`tools/`；迁移完成后删除原文件，不留同名包装层 |
| `cloud/esp32/agent.py`、`agentcore/mode/reactive.py` | Agent 决策与状态进 `agents/lamp/{graph,state,workflows}/`；纯设备状态仍在 `cloud/esp32/state.py` |
| `cloud/go2/agentcore/mode/{reactive,free_explore}.py` | 自主决策与动机状态进 `agents/go2/workflows/`；视觉采集、导航和运动实现留在 `cloud/go2/` 设备层 |
| 三份 `agentcore/personality/`、`soul.py` | 资料与特质映射迁入各角色 `personality/`；演化进 Go2 `workflows/`；删掉已无调用的 `soul.py` 桥接 |
| 灯/Go2 `agentcore/memory/`、Go2 `agentcore/prompts.py` | JSON/JSONL/Markdown 文件读写及必要数据模型分别进 `agents/*/memory/`，提示词进 `agents/*/prompts.py`；仅删除已被 Graph/框架替代且无业务语义的手写缓冲/检索 |
| 三份 `agentcore/llm.py` | 合并框架模型构造和配置于 `agents/common/models.py`，角色模型参数在各自 Graph 装配时声明 |
| `agentcore/perception/`、`cloud/go2/space_context.py` | 传感器/相机采集归设备层 `perception/`；面向模型的空间视图归相应 Agent 的 `tools/` 或 `prompts.py`，不在外层保留 Agent 提示词拼装 |
| 三份 `agentcore/tools/`、管家 `skills/coordination.py` | LangChain 工具及组合技能分别进 `agents/*/{tools,skills}/`；设备安全执行器如仍被 HTTP 直控复用，移到外层设备服务一次实现，由 Agent 工具调用 |
| `cloud/agent_runtime/` | 被框架替代的循环、运行记录、回复和进度调度删除；必要的产品配置校验或 UI 事件映射归入明确的 `agents/common/` 或 `api/`，不保留“另一个运行时”目录 |

导入方向固定为 `API/MQTT 入口 → agents/*/graph.py → agents/*/{tools,skills} → 设备/空间服务`；设备服务不反向导入 Agent，避免循环依赖。新建或重命名 Python 文件时同步修改文档、测试和部署入口；最终 `rg` 不出现 `multitask.py`、旧 `agentcore` 决策入口或旧运行时导入。不要把旧 `graph.py` 内容整体复制进新文件，必须按上述职责拆分。

### 3.2 一个框架内的三份 Agent

- 三者都使用同一套框架装配方式：模型、角色动态系统提示词、工具列表、`ExecutionContext`、文件 checkpointer、流式事件投影和统一日志采集。各角色的 `graph.py` 是主对话唯一入口，自主 Graph 收在本角色 `workflows/`；不再有 `multitask.py` 或自写 `decide/execute/run_loop/RunStore`。
- 管家工具继续以 Card 为依据；灯只暴露灯及允许的同伴工具；Go2 只暴露当前 Go2、导航、视觉、记忆及同伴工具。工具参数用 Pydantic/官方 tool schema 声明，不再把 JSON Schema 字符串拼进提示词、再手工 `json.loads` 解释模型动作。
- 每次模型调用前读取最新人格和当前可信空间状态。管家沉稳、灯高傲、Go2 活泼的默认文案及用户修改值保持原样；特质仍作用于原来的澄清、调光评估、探索/社交候选，不能改变硬性安全限制。
- 普通对话、设备动作、委派、失败和自主发言都经过各自 Graph 的事实约束回复阶段。用 LangChain 的结构化输出传递“哪些步骤已确认、哪些未知、最终文字”；对文字做必要的领域事实校验，不能把自由文本当物理完成证据。旧的第二次人格化调用如仍是现有展示所需，作为 Graph 回复节点保留语义，避免改变角色口吻。

### 3.3 物理动作与暂停的边界

一个有物理效果的模型轮次最多下发一个动作，然后观察真实结果再决定下一步。先用当前 Ark `deepseek-v4-flash/pro` 做**原生 tool calling、单调用约束、结构化输出及流式事件**探针；若模型不能稳定满足，不再写一套严格 JSON 动作解析器，应调整框架模型配置/选用兼容模型，并用框架工具节点前的确定性安全门拒绝多动作派发。不能让 `ToolNode` 默认并行执行多个物理动作。

模型要求补充信息时，由无副作用的 `ask_user` 工具或澄清节点调用 `interrupt()`；API 用原 `run_id` 找到相同 Graph thread，再用 `Command(resume=...)` 续接。`interrupt` 所在节点可能重跑，因此澄清节点前不做物理动作。[官方明确要求中断前副作用可幂等](https://docs.langchain.com/oss/python/langgraph/interrupts)。

Graph 检查点**不等于设备恰好执行一次**。ESP32 MQTT QoS 1、HTTP 超时、Go2 运动都仍以稳定 `tool_call_id → task_id/request_id`、目标任务账本和实体回执为准。派发后到结果写入前宕机时，恢复先查询原 ID；无法确认则显示 `unknown/timeout` 并停下依赖步骤，绝不能换 ID 再发。`ok`、`accepted`、`executing`、`timeout`、`cancelled` 等现有结果含义不变。

### 3.4 持久化与跨进程

**不引入 PostgreSQL、SQLite、向量数据库或其他后端数据库。** 继续用当前 JSON/JSONL/Markdown 文件持久化人格资料、长期记忆、摘要、运行映射和领域数据；迁移只调整文件组织与读写接口，不要求把现有记忆搬到数据库。短期会话和 `interrupt()` 恢复需要持久化 Graph 检查点：在 `agents/common/` 实现一个**薄的文件存储适配器**，遵循 LangGraph `BaseCheckpointSaver` 的同步/异步方法与 `self.serde` 序列化契约，把 checkpoint、父 checkpoint、pending writes 和必要索引保存在共享 JSON 文件中。它只负责文件 I/O，不实现 Agent 循环、记忆推理或任务调度。[官方自定义 checkpointer 契约与一致性测试](https://docs.langchain.com/oss/python/langgraph/checkpointers)。

独立编排器进程和 API 进程使用同一持久目录/挂载卷。文件按稳定的 `thread_id/checkpoint_ns/checkpoint_id` 定位，以安全路径编码避免目录穿越；跨进程文件锁、临时文件写入后原子替换、崩溃后完整性检查和保留期清理必须通过测试。同一 thread 的写入须串行化，读取可见完整检查点及 pending writes；新进程能恢复同一澄清点。文件后端需通过官方 `langgraph-checkpoint-conformance` 基础测试，并在目标 Python/框架版本上验证两进程并发、断电式中断/重启和未决任务恢复。`InMemorySaver` 只用于单元测试；不把测试内存数据当成生产持久化。若所选框架版本改变 checkpointer 契约，先完成适配与验证再切换，不能以无持久化运行替代。[官方 checkpointer 测试说明](https://docs.langchain.com/oss/python/langgraph/checkpointers)。

`thread_id` 按 `agent_id + session_id` 隔离用户对话，同一线程一次只接纳一个活动请求；委派和自主任务使用独立的稳定请求/活动 ID，保存 `run_id/trace_id/parent_run_id` 关联。API 只保存必要的 `run_id → thread_id` 路由索引，不再维护第二套步骤/回复账本。Graph 状态保存消息、已确认工具结果、未决 ID 和澄清点；密钥、原始图像和整段设备数据不进入检查点。

灯/Go2 情节记忆和每日摘要沿用各自现有 JSON/JSONL/Markdown 文件及角色隔离；若 Graph 节点确需 LangGraph `BaseStore` 接口，仅增加读写这些文件的薄适配器，并按官方 Store 语义验证，不维护第二份记忆。非持久会话沿用现有隔离与清理规则，不在本次增加访客专属 Agent 流程。文件搬迁脚本保留时间戳、来源和 7 天保留语义；空间地图、人员库、教学规则和设备物理任务账本仍由原领域文件存储负责。管理员人格资料作为产品配置保留现有值、revision 和编辑 API；内部文件位置可随目录迁移，但 Graph 每次读取最新资料，不能以默认值覆盖用户设置。

### 3.5 全链路可见日志

三个 Agent 的**主对话、委派、灯 reactive/旁白、Go2 reactive/free_explore/视觉触发与每日摘要**都使用同一事件采集契约。沿用 `cloud/observability/events.py` 和 `logging_config.py`，以 `logs/session-<session_id>.jsonl` 作为可检索的结构化事件流；无用户会话的自主任务按稳定活动/任务 ID 记录，避免全挤在 `session-system.jsonl`。`make logs` 及 `scripts/trace_task.py` 扩展为可按 `agent_id`、`run_id/trace_id` 和 `task_id` 查看逐步轨迹，常规 `api.log`/`orchestrator.log` 保留运行错误。日志是运维可见性，PWA 仍只接收当前进度与回复事件，不显示内部 Graph 状态。

每一步至少记录 `ts、agent_id、session_id/thread_id、trace_id、run_id、parent_run_id、step_id、节点/阶段、输入来源、状态、耗时`，并在适用时带 `tool_call_id/task_id/request_id`。事件覆盖：请求/传感器输入、人格版本加载、模型输入上下文和输出、可观察的规划/工具选择、工具输入、委派与设备下发、真实回执或错误、节点转换、checkpoint 写入、暂停/恢复、回复草稿与事实校验、最终发给用户或活动流的文字及对应 SSE `event_id`。管家→灯/Go2 用父子 trace 串起来；每个 Agent 自己的输入、选择、动作和回复可独立查看。这里的“思考”指模型**可观察的决策、规划摘要及工具选择**；不假设模型服务会提供不可见的内部思维链。[LangGraph 流式事件类型](https://docs.langchain.com/oss/python/langgraph/streaming)。

日志要能还原一次运行：文本输入/输出及工具参数/结果在授权的本地详细日志中可读；密钥、鉴权头、私人记忆和二进制图像/音频按现有隐私边界脱敏或只留引用、大小与哈希，脱敏原因显式标记。当前事件记录对长字段有截断上限，迁移时须为必要的文本输入/输出提供受控的完整记录或可关联的分片/附件，不能只剩预览而声称“全步骤可见”。JSONL 写入跨进程安全、日志目录权限和轮转/保留期延续当前机制；采集失败要有可查错误，不能悄悄丢失执行轨迹。不要通过额外一次 LLM 调用来伪造“思考日志”。

## 4. 分阶段实施与验收

### 阶段 0：冻结可观察基线与框架能力探针

1. 记录三个 Agent 的当前人设全文、trait 值/revision、工具与 Card 清单、`manual/reactive/free_explore` 模式、管理员 API 响应、SSE 事件样本和 PWA 截图；另留一组现有基础鉴权/会话隔离样本。历史 JSON/JSONL/Markdown 运行记录和记忆先备份，采样当前 `make logs` 与 session JSONL 的可见内容及缺口，并记下迁移前 Git commit 和目录/代码量基线，供最终对比报告复算。
2. 在隔离环境用当前 Ark 模型、已选定的 LangChain/LangGraph 兼容版本验证：标准 tool calls、单动作约束、Pydantic 参数、`create_agent` 多轮、结构化回复、流式事件、`interrupt`/恢复；记录模型不兼容时的替代模型配置。版本锁入 `pyproject.toml`/`uv.lock` 的变更留待实施，不在本计划中执行。
3. 建立“能力与展示契约”样本：每个公开技能的完成依据、三种口吻的对话/成功/失败例子、`agent_reply/agent_progress/run_stage/reply_error` 字段、`event_id` 去重、trace 关联、前端过程文字和聊天/活动气泡顺序。

**门槛：**原生框架工具调用和共享文件 checkpointer 原型通过；每个设备动作都能说清 `ok` 的外部证据及对应日志；未通过则先解决模型/文件适配兼容，不以自写 JSON Agent 循环兜底。

### 阶段 1：框架基础与领域工具

1. 增加所需的官方 `langchain`、`langgraph` 依赖与 `langgraph-checkpoint-conformance` 测试依赖，不引入数据库驱动。按第 3.1 节建立 `cloud/agents/{common,steward,lamp,go2}/`，完成第 3.4 节的共享文件 checkpointer、稳定 thread 命名和 `ExecutionContext` 传递，先固定导入方向再搬文件。
2. 把现有灯、Go2、管家工具声明映射为各自 `agents/*/tools/` 的 LangChain `@tool`/Pydantic schema；复用原设备执行器、Card 校验、同伴网关及任务账本，随后把仍供 HTTP 直控使用的执行器落到设备服务目录。工具内部重新校验权限和实时设备状态，模型只看到允许的工具。
3. 用框架中间件设置模型/工具调用预算和必要的重试；只对无副作用的模型与读工具重试。物理工具有明确截止时间和结果状态，异常转为结构化工具结果。框架流事件经薄适配器投影到现有 SSE/MQTT 反馈，不把框架内部消息直接发给浏览器。同步扩展现有 `cloud/observability/`、`make logs`/`trace_task.py`，按第 3.5 节记录 Graph/模型/工具/设备的逐步事件，并处理当前长文本截断问题。

**门槛：**假模型可完成“查状态→动作→看回执→继续”，多物理工具调用不并发；权限拒绝、模型异常、超时、跨进程文件检查点读取均可复现；日志按同一 trace 能看到输入、模型选择、工具参数/回执、节点状态和最终输出。

### 阶段 2：灯 Agent 的全部路径

1. 灯设备聊天、管家 `adjust_lighting` 委派、用户直接物理控灯后的灯旁白统一接入 `agents/lamp/graph.py` 和相应 `workflows/`；保留现有路径、HTTP 字段、MQTT `task/result` 和 SSE 内容。聊天与动作旁白共用人格/事实回复节点，不复制一套手写回复循环。
2. `reactive` 感知、活动内社交判断与灯自主回复进入灯 Graph；自主决策状态和冷却/让位截止时间由 Graph 状态持有，传感器监听只负责输入事件。`manual` 默认、调光敏感度、用户/同伴让位和冷却的现有业务规则不变，物理动作仍走同一执行器。
3. 将灯会话历史接入文件检查点，情节/摘要沿用原 JSON/JSONL/Markdown 文件并经 `agents/lamp/memory/` 接入；管理员原有授权持久化、记忆查看、设置页人格资料及灯实体状态展示必须保持；现有非持久会话的隔离和清理不回退。

**门槛：**灯页问答、主聊天委派、控灯旁白和自主活动都显示同一灯的口吻；灯不报告无意义的亮度参数；未收到 ESP32 `result` 时不说已调好；模式切换和用户让位无回归；四条路径的输入、决策、调灯请求、ESP32 回执和回复均能按灯的 trace 查询。

### 阶段 3：Go2 Agent 的全部路径

1. `/chat`、`/chat/stream`、公开技能委派、取消/任务查询接入 `agents/go2/graph.py`；姿态、定时运动、导航、视觉、身份、位置、规则、灯委派和同伴消息仍经过原执行器与安全边界。
2. Go2 `reactive`、`free_explore` 的**模型决策、动机状态、社交、视觉描述、自主回复与决策冷却**进入 LangGraph 工作流；视觉回调和时钟只向 Graph 投递事件，视觉采集、地图、A*、里程计、运动 FSM/租约与急停保留为设备层。好奇心、外向性、大胆程度仍在现有机会和候选中生效。
3. 情节记忆、每日摘要和演化 LLM 调用迁至框架节点/工具，记忆与摘要仍写 JSON/JSONL/Markdown 文件；人员库与地点资产维持领域文件存储。保留既有管理员可见的记忆内容与人格编辑结果。

**门槛：**Go2 页与流式回复的结果一致，断连/急停后无续发动作；“先观察再行动”只在观察结果后推进；探索、社交和日常回复保持 Go2 的童稚口吻；旧文件可读取或完成无损迁移；聊天、视觉、自主探索与摘要可分别查到模型输入输出、动作和真实回执。

### 阶段 4：管家、跨智能体与主聊天

1. 管家普通意图、同伴请求和消息入口接入 `agents/steward/graph.py`；保留 Card 动态发现、规则待确认、活动启动、空间约束以及 MQTT/HTTP 两类设备调用。`invoke_skill` 只调用当前 Card 的公开技能。
2. 跨智能体委派用框架 thread/子运行关联任务状态；MQTT `AgentMessenger` 只负责签名传输、在线校验、消息去重和协议边界，原来承担的 Agent 对话轮次/恢复状态改由 Graph 检查点管理。仍限制单跳、来源权限及父子任务截止时间。
3. API `/api/intents` 的追问续接改为 Graph `Command(resume=...)`；编排器独立进程的 MQTT 消费保持非阻塞。现有主聊天最终由管家收尾，灯/Go2 的子回复仍作为各自气泡展示。

**门槛：**“先关灯，再让 Go2 打招呼”必须先见灯 `ok`；灯离线/结果未知时不派发依赖动作；同伴发言不能升级为用户授权；主聊天的过程、设备气泡和管家收尾顺序与现有前端一致；父子 trace 能追到管家规划、灯和 Go2 各自的输入输出及最终收尾。

### 阶段 5：统一展示、删除旧实现与发布

1. 用相同的 `agent_id/scope/source/status/run_id/trace_id/parent_run_id/step_id/event_id/reply_kind/stage` 投影 Graph 事件；比对 `useSendIntent.js`、`app.jsx`、设备页聊天和 `ActivityFeed.jsx` 的截图及事件回放。PWA 只做确有必要的兼容修正，不改页面内容和呈现方式。
2. 仅将 `data/runs` 中 Graph 恢复必需的会话/未决状态转换为 JSON 检查点；灯/Go2 情节记忆与摘要保持文件格式，必要时只调整目录和字段。先 dry-run 核对数量、时间戳、会话归属和未决任务 ID；停写、备份、转换、抽样回放后切换。未决物理动作只按旧 ID 查询，不能因迁移重复执行。
3. **三条路径全部接通且验证后，在同一次正式切换中删除** 三个 `multitask.py`、旧 `cloud/*/agentcore/` 决策文件、`cloud/esp32/agent.py`，以及 `cloud/agent_runtime/{loop,store,replies,progress}.py` 和只服务于它们的 `progress_facts.py`、`json_files.py`（若任务账本/配置仍依赖，先移到领域存储模块）；删除旧 JSON 工具调用解析、Go2/灯自写记忆缓冲与相关度排序、每日摘要的独立模型循环、自主决策循环、重复回复逻辑和已废弃测试。`cloud/agent_runtime/personality.py` 若仍承担管理员配置校验/revision，移至明确的产品配置模块；不留 `agent_runtime` 目录。检查 `rg` 不再有旧运行时或 `multitask.py` 导入；更新 `README.md`、`AGENTS.md`、架构及运维文档，且依赖锁只有一套 Agent 运行框架。
4. 一次性部署三角色新入口。正式运行进程不保留“旧循环/新 Graph”双入口、长期 feature flag 或按智能体混用框架。旧版代码可通过 Git 回退，数据备份在核对完成前保留；生产期间不把旧代码作为隐藏降级路径。

**门槛：**现有全部云端测试通过，新增文件 checkpointer 官方一致性、跨进程恢复/设备回执/基础鉴权/事件契约与日志完整性测试通过；真实 ESP32 和 Go2 短场景、管理员 PWA 主聊天与两个设备页现场核对通过，目标目录经检查无旧入口及循环依赖，并完成第 6 节对比报告后，才清理历史运行数据备份。

## 5. 必测场景与发布判据

| 场景 | 必须看到的结果 |
| --- | --- |
| 三人设编辑后聊天、动作成功、失败、离线、部分成功 | 管家沉稳、灯高傲刁钻、Go2 活泼童稚；`prompt/traits/revision` 不丢；回复不冒出无意义的色温/亮度原始参数 |
| 一步和多步设备动作 | 每次物理动作前校验权限/状态；依赖动作等前一步实体回执；`accepted/timeout/unknown` 不升级为完成 |
| 模型输出两个工具、无效参数、空输出 | 不并发派发物理动作；没有合法工具消息就不执行设备；预算及失败语义一致 |
| 澄清、浏览器重连、进程重启、重复 POST | 同一 `run_id`/thread 恢复，原动作不重复；SSE 用稳定 `event_id` 去重，页面只显示一次回复 |
| Go2 运动中断、断连、导航失败 | 急停/租约仍有效；Graph 取消不被误当物理停止确认；位置未知时不称到达 |
| 灯 `manual/reactive`、活动内自主发言和用户介入 | 原模式、冷却与让位不变；自主发言归入现有空间活动流，不混入其他用户会话 |
| 管理员与同伴来源、基础会话隔离 | 管理员原有工具、记忆和设置体验不变；同伴无权冒充用户；现有 API 鉴权和跨会话隔离保持，暂不扩展访客专属体验 |
| 主聊天、灯页、Go2 页、活动流截图与 SSE 回放 | 原有气泡归属、过程小字、结果文字类别、设置组件和设备控件不变；不显示 Graph/工具内部字段 |
| 三智能体日志与跨角色委派 | 管家、灯、Go2 的对话和自主路径都有输入→可观察规划→工具/设备回执→回复轨迹；父子 trace 可串联；长文本有完整受控记录，敏感字段明确脱敏；`make logs` 可按 Agent/运行 ID 检索 |
| 文件持久化和跨进程恢复 | 人格、记忆、摘要、运行映射仍为 JSON/JSONL/Markdown；官方 checkpointer 一致性测试通过；API/编排器重启后恢复澄清与未决任务，不重复物理动作；没有数据库服务或数据库驱动 |

**发布判据：**所有旧运行时代码已无生产调用；三个 Agent 的所有对话和自主模型路径都经 LangChain/LangGraph；无后端数据库，文件数据转换可核对、未决任务无重发；每条路径的逐步日志可查；管理员体验、上述回归、真实设备和 PWA 核对均通过；第 6 节对比报告如实完成。只跑单元测试不能替代设备与前端验收。

## 6. 必交付的迁移前后对比报告

迁移完成时新增 `docs/architecture/agent-langgraph-migration-comparison.md`，并为其配置 `.gitignore` 例外，作为同一交付的一部分，不以口头总结替代。阶段 0 先记下基线 commit、文件清单和测量口径；最终报告同时写明迁移后 commit。两个版本用**同一工具、同一范围**测量，生产 Python 与测试代码分开统计，排除 `.venv`、`data/`、生成文件和文档；不能只挑被删除的文件制造“代码量下降”。

报告至少包含以下内容：

1. **入口与文件映射**：迁移前/后目录树；三个 `multitask.py`、`agent.py`、旧 `agentcore`、`agent_runtime` 的去向；API/MQTT/设备层保留的文件；新增、移动、合并和删除清单。每个角色的人格、记忆、工具、公开 skill、自主工作流都能从一个角色目录找到。
2. **调用链对比**：迁移前的 `API/MQTT → multitask.py → 自写 loop/store/replies` 和迁移后的 `API/MQTT → agents/*/graph.py → LangChain tools/LangGraph 文件 checkpoint → 原设备执行器`；标明 JSON/JSONL/Markdown 记忆、PWA 事件投影、Card 委派和真实设备回执的位置。
3. **代码量与依赖**：按角色 Agent 包、共享 Agent 代码、外层适配器和整个 `cloud/` 四个范围统计文件数、Python 物理行数、非空非注释行数、测试行数及变化百分比；单列文件 checkpointer/日志适配代码量，附 `git diff --stat/--numstat` 与最终依赖列表，证明没有新增数据库依赖。若总代码量增加，要具体说明增加在哪里及为何必要，不能预设用了框架就一定减少。
4. **组织与维护成本**：核对是否仍有重复工具 schema/回复/循环、跨包反向导入、未使用旧模块；给出“新增一个原子工具、增加一个公开 skill、修改一种人格特质”分别需触及哪些文件的前后对比。用实际路径证明结构更集中、职责更清晰；若未改善则列为未完成项。
5. **行为与显示证据**：列出三个 Agent 的能力/人设/自主模式回归结果、真实设备回执、管理员三处聊天与活动流截图、SSE 事件样本、端到端测试数量和任何尚未验证的限制；不得把测试桩通过写成实机通过。
6. **文件持久化与日志证据**：列出迁移前后 JSON/JSONL/Markdown 存储路径、格式和样本数量，文件 checkpointer 一致性/跨进程恢复结果；给出三角色对话、自主和跨角色委派的脱敏日志样本、检索命令、事件覆盖率，以及 `make logs` 的前后可见性差异。

报告结论分别回答：**改动集中在哪些模块、整体代码量究竟增减多少、框架替代了哪些自建能力、目录是否更好找、更容易扩展、JSON/Markdown 文件持久化是否保持、逐步日志是否真正可见、管理员体验与前端显示是否保持原样**。这些判断以可复算的表格、文件路径和日志样本为依据，不用笼统的“架构更优”代替证据。
