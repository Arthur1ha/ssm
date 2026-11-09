# 云端智能体目录与设备互通

本文按当前代码说明 `cloud/agents/` 的职责，以及进展事件和设备间消息的边界。

## 总体分工

`cloud/agents/` 放云端智能体的决策代码。管家负责协调；灯的硬件驱动在 `edge/`；Go2 的连接、视觉和运动执行在 `cloud/go2/`。三个角色共用 LangGraph 的任务运行骨架，但各有自己的提示词、人格、工具和自主工作流。

一次请求通常经过：入口 → 角色 `graph.py` → `common/runner.py` 逐轮决策 → 角色工具适配 → 真实回执 → 角色 `prompts.py` 生成回复。

| 目录 | 职责 |
|---|---|
| [`common/`](../../cloud/agents/common/) | 三个角色共用的 Graph 运行、检查点、执行记录、进展和事实校验；不定义统一人格。 |
| [`steward/`](../../cloud/agents/steward/) | 管家：理解用户目标、发现设备卡、调用公开技能、汇总全局结果。没有自己的物理设备。 |
| [`lamp/`](../../cloud/agents/lamp/) | 灯的云端大脑：调灯决策、传感器触发、自主模式和角色回复。 |
| [`go2/`](../../cloud/agents/go2/) | Go2 的云端大脑：对话、工具选择、记忆、人格和自主探索。 |

`cloud/agents/` 以外仍有当前运行所需的服务和共享模块：

| 目录 | 职责 |
|---|---|
| [`cloud/esp32/`](../../cloud/esp32/) | 灯服务实例、自主模式、传感器事实、执行校验、任务去重与实际回执。 |
| [`cloud/go2/`](../../cloud/go2/) | HTTP 服务、WebRTC、视觉、导航、动作校验和自主事件调度；`mode/free_explore.py` 调用 `agents/go2/workflows/` 的 Graph。 |
| [`cloud/orchestrator/`](../../cloud/orchestrator/) | 独立 MQTT 进程入口、签名请求校验、线程调度、HTTP 派发和反馈；决策交给管家 Graph。 |
| [`cloud/personality/`](../../cloud/personality/) | 三角色共用的人格存储、版本校验和名称管理。 |
| [`cloud/peers/`](../../cloud/peers/) | 同伴发现、签名通信、技能请求、协作权限、取消与互动游戏。 |
| [`cloud/space/`](../../cloud/space/) | 空间成员、可信执行上下文、用户要求、活动时间线和人员库。 |
| [`cloud/observability/`](../../cloud/observability/) | 结构化日志、任务关联事件和脱敏。 |
| [`cloud/storage/`](../../cloud/storage/) | 原子 JSON 文件存储与锁。 |

设备能力与在线状态来自 MQTT retained card/manifest/status；`cloud/space/space.json` 只保存空间成员配置。旧空文件 `cloud/orchestrator/devices.json` 已清理，不再作为回滚资料保留。统一教导位于 `agents/common/training/`，当前没有 `/api/rules` 入口或固件离线自动控灯规则。

## `common/` 文件

| 文件 | 作用 |
|---|---|
| [`runner.py`](../../cloud/agents/common/runner.py) | 共用 LangGraph 任务循环。`GraphRun` 记录目标、步骤、状态和回复；每轮选择一个工具、观察结果后继续。 |
| [`checkpoint.py`](../../cloud/agents/common/checkpoint.py) | 将 Graph 状态持久化到文件，支持跨进程读取与中断恢复。 |
| [`run_index.py`](../../cloud/agents/common/run_index.py) | 将公开 `run_id` 映射到会话 Graph。 |
| [`views.py`](../../cloud/agents/common/views.py) | 按 `run_id` 从检查点读取结果，并固定已完成任务的查询位置。 |
| [`effects.py`](../../cloud/agents/common/effects.py) | 有物理效果工具的持久化预留记录，避免恢复时重复动作。 |
| [`context.py`](../../cloud/agents/common/context.py) | 可信执行上下文与 Graph 会话 ID。 |
| [`models.py`](../../cloud/agents/common/models.py) | 为三个角色创建文本模型，并创建视觉模型。 |
| [`replies.py`](../../cloud/agents/common/replies.py) | 整理真实回执、检查回复格式、提供模型不可用时的回退；角色语气由各自 `prompts.py` 决定，不用关键词判断完成声明。 |
| [`progress.py`](../../cloud/agents/common/progress.py) | 对已观察到的步骤尝试生成一句人格化进展；调用模型，在后台线程运行，有时限，失败或超时可以不发。 |
| [`progress_facts.py`](../../cloud/agents/common/progress_facts.py) | 从同一步骤确定性提取少量结构化字段，供前端生成状态文案；不调用模型。 |
| [`training/service.py`](../../cloud/agents/common/training/service.py) | 统一教导的草案、确认、运行期加载与执行校验；单纯保存不等于自动执行。 |
| [`training/catalog.py`](../../cloud/agents/common/training/catalog.py) | 从设备卡的状态机转换、参数和观察声明派生可训练目录。 |
| [`training/store.py`](../../cloud/agents/common/training/store.py) | 教导与草案的持久化、版本及分区锁。 |

`common/tests/` 测试检查点、任务循环和执行记录等；统一教导另有 `training/*_checks.py`。`__init__.py` 标识 Python 包。

提示词按职责维护：角色 `prompts.py` 保留身份、表达和目标边界，`common/messages.py` 统一事实与数据边界，`runner.py` 说明逐轮调用及暂停格式。工具的用途、参数、前置条件和结果含义写在原生工具声明中，由 `bind_tools` 传给模型；Go2 使用 `go2/tools/registry.py`，灯使用 `esp32/tool_registry.py` 和 `lamp/tools/schemas.py`，管家使用 `steward/tools/`，教导使用 `common/training/tools.py`。自然语言回复只做格式检查，实际完成状态始终来自结构化回执；不通过中文正则或关键词猜测语义。

### `progress.py` 与 `progress_facts.py` 的区别

两者消费同一个已经发生的 `step`，但产物不同：

| | `progress_facts.py` | `progress.py` |
|---|---|---|
| 输出 | `tool`、`target_unit_id`、`skill_id`、`arguments`、`reason` 等结构化字段 | 一句智能体第一人称自然语言，放在进展事件的 `text` 字段 |
| 方法 | 纯 Python 提取，结果稳定 | 调用 LLM，结果可变 |
| 时机 | 发布每个进展阶段时同步生成 | 观察到工具结果后异步尝试生成 |
| 失败影响 | 仍能发布基础进展 | 不发这句额外旁白；基础进展仍在 |
| 用途 | 前端按事实生成“调整灯光：已完成”等状态 | 让设备用自己的口吻解释刚才的结果 |

例如一次调灯结果 `ok`，`progress_facts.py` 可以提供工具名、目标和结果；前端据此显示“调整灯光颜色：已完成”。`progress.py` 可能另发“暖白到位了，这样看着才舒服”。两者都不是最终的 `agent_reply`。灯的原始亮度值不再拼进前端进展文案；设备详情页仍可显示参数。

## 三个角色的对应关系

| 职责 | 管家 `steward/` | 灯 `lamp/` | Go2 `go2/` |
|---|---|---|---|
| 对话入口 | [`graph.py`](../../cloud/agents/steward/graph.py) | [`graph.py`](../../cloud/agents/lamp/graph.py) | [`graph.py`](../../cloud/agents/go2/graph.py) |
| 决策与最终回复提示 | [`prompts.py`](../../cloud/agents/steward/prompts.py) | [`prompts.py`](../../cloud/agents/lamp/prompts.py) | [`prompts.py`](../../cloud/agents/go2/prompts.py) |
| 人格与特质 | `personality/profile.py`、`behavior.py` | 同左 | 同左；每日特质演化由 `workflows/evolution.py` 调用 profile 存储 |
| 工具 | 设备卡校验、跨设备派发、协调策略 | 调灯、同伴查询与通信 | 从 Go2 设备工具注册表生成可见工具 |
| 记忆 | 会话历史与空间上下文 | 事件和每日摘要 | 事件、摘要、命名地点及检索 |
| 自主行为 | 协调其他角色 | 传感器和 reactive 调光 | 探索、社交和特质演化 |

管家的 [`skills/coordination.py`](../../cloud/agents/steward/skills/coordination.py) 让可信的同伴协作请求进入管家 Graph；`tools/` 负责设备卡、权限和派发。灯的 `workflows/` 处理聊天之外的传感器与自主任务。

## Go2 文件对照

下列路径相对 `cloud/agents/go2/`。

| 文件 | 作用 |
|---|---|
| [`graph.py`](../../cloud/agents/go2/graph.py) | Go2 对话主入口 `run_go2_loop`：建任务、运行共用循环、执行工具、发布回复。 |
| [`prompts.py`](../../cloud/agents/go2/prompts.py) | 每轮决策提示及 Go2 专属最终回复提示。 |
| [`prompt_contract.py`](../../cloud/agents/go2/prompt_contract.py) | 固定安全规则和提示长度预算；决策提示使用其固定规则，每日摘要使用完整组装函数。 |
| [`run_view.py`](../../cloud/agents/go2/run_view.py) | 根据 `run_id` 查询持久化任务状态，生成稳定回复事件 ID。 |
| [`tools/declarations.py`](../../cloud/agents/go2/tools/declarations.py) | 从设备工具注册表生成模型可见声明，并按可信上下文过滤权限；不直接驱动机器狗。 |
| [`memory/episode.py`](../../cloud/agents/go2/memory/episode.py) | 按日期读写事件记忆。 |
| [`memory/daily_summary.py`](../../cloud/agents/go2/memory/daily_summary.py) | 读取、生成和保存每日记忆摘要。 |
| [`memory/spatial.py`](../../cloud/agents/go2/memory/spatial.py) | 保存、查找和删除命名地点。 |
| [`memory/recall.py`](../../cloud/agents/go2/memory/recall.py) | 按问题筛选有来源标识的相关记忆并限制长度；已接入 `graph.py` 的对话提示组装。 |
| [`personality/profile.py`](../../cloud/agents/go2/personality/profile.py) | 人格文案、特质的持久化和每日演化入口，复用共享 `ProfileStore`。 |
| [`personality/behavior.py`](../../cloud/agents/go2/personality/behavior.py) | 把好奇心、外向性和大胆程度转成探索、互动及表达偏好。 |
| [`workflows/free_explore.py`](../../cloud/agents/go2/workflows/free_explore.py) | 自主探索的动机和任务阶段 Graph；地图与运动安全仍由 `cloud/go2/` 负责。 |
| [`workflows/social.py`](../../cloud/agents/go2/workflows/social.py) | 判断是否向同伴发消息或游戏邀请，发送前重新核对条件；由 Go2 自主模式调用。 |
| [`workflows/reply.py`](../../cloud/agents/go2/workflows/reply.py) | 为自主动作、教导和导航结果生成角色回复。 |
| [`workflows/daily_summary.py`](../../cloud/agents/go2/workflows/daily_summary.py) | 从已记录事件生成每日摘要，避免同一天重复生成。 |
| [`workflows/evolution.py`](../../cloud/agents/go2/workflows/evolution.py) | 根据摘要计算、校验并保存特质变化。 |
| [`workflows/spatial_lookup.py`](../../cloud/agents/go2/workflows/spatial_lookup.py) | 对已保存的地点名称做有边界的模型辅助匹配。 |

Go2 的 `tests/` 包含 `test_daily_summary_graph.py`、`test_evolution_graph.py`、`test_graph_entry.py`、`test_run_view.py`、`test_social_workflow.py`、`test_tool_declarations.py`、`test_workflows.py`，分别覆盖摘要、演化、对话入口、任务查询、社交、工具可见性和自主工作流。各级 `__init__.py` 主要标识 Python 包。

## 设备之间如何互通

**发消息是通用能力，不是灯独有。** 三个角色都有 `send_message` 工具入口：灯在 [`lamp/tools/schemas.py`](../../cloud/agents/lamp/tools/schemas.py)，Go2 在 [`cloud/go2/tools/registry.py`](../../cloud/go2/tools/registry.py)，管家在 [`steward/tools/__init__.py`](../../cloud/agents/steward/tools/__init__.py)。共用的 [`cloud/peers/messaging.py`](../../cloud/peers/messaging.py) 负责身份、同空间和在线检查、签名 MQTT 投递、消息 ID、回复关联与超时；主题格式见 [`protocol/topics.md`](../../protocol/topics.md)。工具会按任务上下文限制可见性，例如灯的纯照明调用只暴露调灯相关工具。

| 能力 | 意义 | 完成条件 |
|---|---|---|
| `list_peers` | 发现同空间在线智能体及公开技能 | 得到最新可见列表；不表示已联系对方 |
| `send_message` | 发送普通消息或游戏邀请 | `accepted` 只表示请求已投递；不表示对方同意或执行完成 |
| `request_peer_skill` | 请求同伴执行设备卡公开技能 | 必须核对目标的真实结果；只有 `ok` 才能作为完成事实 |

LangGraph 库提供子图、handoff，以及 `StateGraph`、`Command`、`Send` 等状态与路由原语；`Send` 调度图节点，不是网络消息投递。额外部署 Agent Server 后，还可通过 RemoteGraph 调用远程 Graph，或使用其 A2A 接口发送消息、流式接收结果、查询和取消任务。SSM 当前只依赖 LangGraph 库，没有部署 Agent Server。

这些能力不能直接替换 SSM 的 MQTT retained/LWT 发现、空间授权、消息过期与重放防护、物理执行去重和真实回执。Graph 检查点不代表硬件动作已经完成；恢复或重试仍需现有执行账本保证副作用幂等。

`peers/inbound.py` 中的 Go2 回复生成、`peers/games.py` 中的多阶段游戏流程是适合进一步移到 `agents/*/workflows/` 的候选；本次仅记录边界，没有迁移它们。`messaging.py`、`view.py`、`policy.py` 等仍承担协议与业务适配，不能通过增加 handoff 包直接删除。

参考：[LangChain 多智能体模式](https://docs.langchain.com/oss/python/langchain/multi-agent)、[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[RemoteGraph](https://docs.langchain.com/langsmith/use-remote-graph)、[Agent Server A2A](https://docs.langchain.com/langsmith/server-a2a)。
