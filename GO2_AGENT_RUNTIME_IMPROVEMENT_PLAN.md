# Go2 智能体运行机制优化计划

> 日期：2026-10-22  
> 状态：待审阅，尚未实施  
> 范围：Go2 的记忆召回、模式归位、对外能力映射、工具执行和 prompt 管理；保持现有 MQTT 控制面与 HTTP/SSE 数据面。

## 1. 目标与依据

目标是让 Go2 对同一条指令稳定地完成「选择相关上下文 → 规划 → 校验并执行 → 核对结果 → 回复」，并让设备页对话、主聊天委派、SSE 对话和两种自主模式共享必要的执行约束。优先解决动作状态误报、端到端超时、参数与权限检查分散、上下文无预算等可验证问题。

参考资料是第三方仓库 `liuup/claude-code-analysis` 对 Claude Code 的静态分析：[Memory](https://github.com/liuup/claude-code-analysis/blob/main/analysis/04-agent-memory.md)、[Skills](https://github.com/liuup/claude-code-analysis/blob/main/analysis/04c-skills-implementation.md)、[Tool Call](https://github.com/liuup/claude-code-analysis/blob/main/analysis/04b-tool-call-implementation.md)、[Prompt](https://github.com/liuup/claude-code-analysis/blob/main/analysis/04g-prompt-management.md)。这些文章用于提炼设计原则，Go2 的行为和接口以本仓库代码为准。

### 与 Claude Code 的对照边界

对照的是职责划分和执行治理，不是目录名逐个照搬。Claude Code 官方把常驻上下文、按需加载的 Skills、外部工具连接、子智能体和事件钩子作为不同扩展点；其 Skill 主要是按需提供给模型的 Markdown 知识或工作流。[官方扩展说明](https://code.claude.com/docs/en/features-overview) Go2 的 `reactive`、`free_explore` 是长期运行的自主模式；Go2 未来的内部 Skill 是可复用的任务流程；Agent Card 的 `skills` 又是对外 API 入口。这三者用途不同，不能因 Claude Code 有 `skills/` 就把 Go2 的模式也放进去。

Go2 与 Claude Code 可比较的部分是：工具声明和执行、记忆召回、prompt 组装、可复用工作流。Go2 特有的 WebRTC 连接、运动控制、导航闭环、持续自主模式和 MQTT/HTTP 双面接口，应按机器狗运行需要组织，不套用 Claude Code 的 Shell/MCP 插件结构。

### 当前基线

| 方面 | 当前实现 | 可验证的问题 |
|---|---|---|
| Tool | `agentcore/tools/tools.py` 存函数表和描述；`agent.py` 有普通与 SSE 两段执行循环 | 描述、Agent Card schema、实际参数约束分离；规划提示要求最多 3 次调用，但代码未硬限制；返回字符串靠前缀猜状态。`go2_sport` 的「已发送，未确认」字符串可被归为 `ok`。 |
| Memory | `episode.py` 按天存 JSONL，`daily_summary.py` 存日摘要，`spatial.py` 存地点；访客会话另有内存历史 | planner 每次附加最近六天摘要和当天全部事件；长日记录没有预算，问题与记录也没有相关性筛选。 |
| Mode / Skill | Agent Card 公开 4 个 `skill_id`；`agentcore/skills/` 混放视觉、身份和 `ReactiveMind`；`navigation/drive.py` 实现 `Drive` | 视觉和身份偏底层服务；`ReactiveMind` 与 `Drive` 是各自独立的自主模式大脑，不应改称 Skill；Card 声明、`agent.py::_SKILL_TO_TOOL` 和实际工具还需手工同步。 |
| 入口与超时 | 设备页经 `/api/commands/go2` 直达 Go2；主聊天经 `/api/intents`、编排器和 Card 委派；Go2 还有 SSE 对话接口 | 设备页内部 HTTP 超时 25 秒，主聊天普通 HTTP skill 超时 10 秒，单个 WebRTC 动作的完成等待可达 20 秒；超时后任务是否仍在执行需要可见。 |
| Prompt | 性格在 `soul.py`，各调用点各自拼接任务提示 | planner、回复、视觉、自主反应使用不同组装方式；缺少稳定规则与动态数据的清晰边界及版本记录。 |

## 2. 总体设计约束

1. Go2 仍由现有 Planner → Executor 图处理用户指令。设备页只与 Go2 交互：`ChatPanel → /api/commands/go2 → /api/go2/chat → Go2 Agent`，当前不传 `skill_id`，由 Go2 Planner 选择工具。主聊天负责多智能体协作：`/api/intents → 编排器 → Card skill → Go2/其他智能体`。编排器通过 Agent Card 发现能力，经 HTTP 调用 Go2；控制面 card/status 继续走 MQTT。两条入口共用 Go2 的权限、记忆、工具执行与结果语义。
2. 保留 `ExecutionContext` 的管理员与访客权限、`ControlLease` 的运动租约，以及设备 FSM 的最终动作约束。新增校验必须在实际工具调用前完成，不能只依赖 prompt。
3. 结果沿用现有 `ok / accepted / executing / blocked / rejected / timeout / cancelled / error` 语义，并为缺少必要信息且尚未执行的请求定义 `needs_clarification`。只有获得设备完成反馈才能返回 `ok`；仅收到命令或发出停止命令时返回 `accepted`。前一个运动动作仅为 `accepted` 时，不自动开始依赖它完成的下一步运动；停止与状态查询仍可执行。
4. 按下节的职责关系整理现有代码。`reactive` 和 `free_explore` 是两个互斥的自主模式，各有独立的大脑和运行循环，归入 `cloud/go2/agentcore/mode/`；它们可以调用 tools，但不包装成 Skills。Skill 仅表示以后经过验证、可按需调用和复用的有界任务流程；tool 负责一次有明确输入输出的操作。
5. 每个任务改动后运行定向测试，能在约 5 分钟内用模拟设备或 API 响应观察核心结果。实机运动测试另列为部署验收。

### 已有能力按什么职责归位

| 职责 | 判断标准 | 现有能力的归位 |
|---|---|---|
| 底层服务 | 持续提供数据或封装设备算法，不决定一个用户目标的完整步骤 | `vision.py` 负责画面检测与分发；`identity.py` 负责识别状态和人员 provider；`navigation/navigator.py` 负责路径与运动闭环；memory 负责存取。视觉、身份代码从当前 `skills/` 归入 `agentcore/perception/`。 |
| Tool | 一次可校验、可执行、可报告结果的操作；可以调用复杂的底层服务 | `go2_sport`、`go2_observe`、`go2_recognize_person`、`go2_navigate_to`、`go2_status` 等。简单指令可以直接选 tool。 |
| 自主 Mode | 长期运行、按设备状态和环境事件持续决策，由模式开关控制启停 | `ReactiveMind` 负责 `reactive` 的视觉变化、规则/LLM 决策和冷却；`Drive` 负责 `free_explore` 的动机状态机、探索和社交决策。分别迁入 `agentcore/mode/reactive.py`、`agentcore/mode/free_explore.py`；`manual` 是关闭自主决策的模式，无需单独大脑。 |
| 内部 Skill | 一个可按需调用、可复用且有结束条件的目标流程；可多次调用 tools | 本期不强行把现有模式改造成 Skill，也不预设首批 Skill；以后出现经验证的多步任务再加入。 |
| Agent Card skill | 对外可发现、可独立调用的服务菜单；入口可以指向内部 skill 或单个 tool | 现有 `go2_chat`、`go2_sport`、`go2_navigate`、`go2_recognize_person` 保持 ID 稳定。Card 字段继续叫 `skills`。 |

调用关系分别是「设备页消息 → Go2 Planner → tools」、「主聊天意图 → 编排器选 Card skill → Go2 Planner/指定 tool」和「视觉事件/模式时钟 → 对应 Mode 大脑 → tools」。以后出现内部 Skill 时，Planner 或 Mode 可以按任务调用它，再由 Skill 调用 tools。Card 只决定对外公布哪些入口，不要求每个菜单项都对应一个内部 Skill。

Agent Card 的协议字段继续用 `skills`：A2A 规范中的 [`AgentSkill`](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto) 可描述一项具体操作或较完整的行为；同一规范的 `capabilities` 表示 streaming 等协议特性。当前 SSM 的 Card 还带有本项目自定义的 `params_schema`、`invoke` 等字段，实施时需保持现有消费者兼容。

例一：编排器选 Card 的 `go2_navigate`，Go2 调用 `go2_navigate_to` tool，tool 再调用导航器。这是一次明确操作，不必额外套 Skill。例二：`reactive` 模式收到视觉变化，其独立大脑判断规则与冷却、选择动作，再调用 `go2_sport` 或 `go2_move` tool。例三：`free_explore` 模式的大脑在多轮观察、导航、社交和用户中断之间推进自主运行。

现有 `skills/` 是历史目录名：`vision.py`、`identity.py` 是服务，`reactive.py` 是模式大脑；另一个模式大脑目前放在 `navigation/drive.py`。Task 3 让目录和职责对齐；导航算法仍留在 `navigation/`。只有可独立复用的有界任务才放进未来的 `skills/`，本期不保留空目录占位。

### Go2 目标目录与迁移边界

```text
cloud/go2/
├── agent.py                 # Go2 对话入口和图装配；规划、回复调用下方模块
├── router.py                # HTTP 路由与依赖装配
├── card.py                  # Agent Card 构建与控制面发布
├── control.py / execution.py / paths.py
├── agentcore/
│   ├── mode/                # reactive.py、free_explore.py：两个独立自主大脑
│   ├── perception/          # vision.py、identity.py：感知与身份状态服务
│   ├── tools/               # 工具实现、声明 registry.py、统一 executor.py
│   ├── memory/              # episode.py、spatial.py、daily_summary.py、recall.py
│   ├── prompts.py           # 稳定规则与各任务模板的组装入口
│   ├── llm.py               # 文本/视觉模型客户端配置
│   ├── rules.py             # 视觉触发规则的存取与匹配
│   └── soul.py              # 性格特质与演化
├── connection/             # WebRTC、FSM、传感器、视频
├── navigation/             # 寻路、地图、导航闭环；不放自主模式大脑
└── data/                   # 运行期记忆/规则/性格；路径由 GO2_DATA_DIR 配置
```

这是一张职责图，不要求一次性搬完。当前 `connection/`、`navigation/` 的领域划分，`memory/` 的存储目录，以及通过 `GO2_DATA_DIR` 配置运行数据的位置都已经合理，优先保留。当前 `tools.py` 还混有 LLM 客户端和规则存取，应拆到各自模块；`agent.py` 的普通/SSE 执行应复用同一执行链；`router.py` 约 1,095 行，同时装配 Card、连接、运动、导航、视觉和对话接口，先抽 Card 构建/发布，其他路由按维护需要再拆。迁移时保持公开 HTTP 路径、Card 字段和现有导入调用的行为可验证。

### 训练成果怎样沉淀

| 训练或交互后得到的内容 | 归属 | Go2 当前例子 |
|---|---|---|
| 一个地点、人物关系或偏好 | Memory | 命名地点、身份关系、事件摘要 |
| 一个条件触发单次动作 | Rule | `rules.json` 中「看到某物 → 做某动作」 |
| 多步、可重复执行且有失败处理的行为 | Skill | 将来经过验证的「观察 → 判断 → 选择动作 → 核对结果」流程 |
| 一次新的底层可执行操作 | Tool | 接入设备新动作或新传感器操作 |

训练结果可以先作为候选流程记录；验证触发条件、允许 tools、失败与中断处理后，再发布为版本化 skill。规则命中次数或一次成功记录本身不自动把规则变成 skill。

### 哪些 skill 应该出现在 Agent Card

Card 的 `skills` 是**对外入口白名单**，不自动枚举内部 `skills/` 目录或 `TOOL_FN_MAP`。一个条目应同时满足：外部确有独立调用需求；输入输出与权限能清楚描述；调用行为稳定且可验证；相比现有条目有独立用途。一个条目可以由单个 tool 实现，也可以由多 tool skill 实现。多个相近的基础动作可在一个条目下用参数区分。

当前 4 项是基线：`go2_chat` 处理自然语言，`go2_sport` 以 `cmd` 覆盖站立/招呼/跳舞等预定义动作，`go2_navigate` 到命名地点，`go2_recognize_person` 识别当前人物。`go2_status` 放在设备状态/telemetry；`reactive` 和 `free_explore` 是经 modes 启停的自主流程；视觉观察等可由对话入口按需使用；人员库、规则和地点的管理操作继续走对应的受权入口。

**覆盖检查：**内部 tool 不在 Card 时，编排器必须有一个语义明确的公开入口能把用户目标送到 Go2。例如「前进 2 秒」现在可能经 `go2_chat` 进入 Go2 Planner，再选内部 `go2_move(direction, speed, duration)`；但 Card 的对话描述较宽泛，编排器的路由提示也没有明确说明短距离移动属于它，不能依赖这条间接路径稳定选中正确工具。短距离移动是独立的外部目标，建议在 Task 1 完成运动校验后增加**一个** `go2_move` Card skill，声明方向、速度和时长及其界限，不按方向或动作数量逐项发布。

「移动两步」还涉及执行语义：当前 `go2_move` 只按时长运动，不能确认实际走了两步；用户也未指定方向。Go2 应先澄清方向与目标距离/时长，或明确说明只能近似移动，不能默默把“两步”换算成秒数。未来若需要按实际位移精确执行，应单独设计基于里程计反馈的相对移动能力，并验证停止误差。

澄清请求使用结构化结果 `{"result": "needs_clarification", "question": "...", "missing_fields": [...]}`。Go2 不调用运动工具；设备页显示 `question`，主聊天由编排器原样转达。待澄清目标按会话和设备短暂保存，下一轮补齐后重新校验、规划；过期或切换话题则丢弃。若缺的是当前硬件无法提供的「精确步数」，回复能力边界和可选的时长/距离方案，不把它当作一个待补参数。

## 3. 实施顺序

### Task 0：记录基线与固定回归样例

**文件：**现有 `cloud/go2/tests/test_agent.py`、`test_access_control.py`、`test_card.py`；必要时新增小型测试夹具。

- [ ] 运行 `uv run python -m pytest cloud/go2/tests -q`，记录当前失败及其原因。
- [ ] 固定输入输出样例：设备页自然语言聊天（无 `skill_id`）、主聊天经 Card 委派的指定 `skill_id`、SSE 对话、访客受限工具、动作仅被接收、工具参数错误，以及「前进 2 秒」和「移动两步」两种运动意图。
- [ ] 记录两条入口的实际超时链：设备页 `CommandBus` 30 秒、API 内部 Go2 请求 25 秒；主聊天 HTTP skill 默认 10 秒、导航 30 秒；Go2 单动作等待最多 20 秒。记录超时后是否仍有任务在设备上执行。
- [ ] 固定 `manual / reactive / free_explore` 三种模式的切换、中断和决策样例；确认 `ReactiveMind` 与 `Drive` 各自的状态及 LLM 决策入口。
- [ ] 用 mock 的 Go2 连接和 LLM 覆盖上述样例；不得接触实机或公网。

**验收：**有一份可重复的 Go2 测试基线；后续任务能单独比较行为变化。

### Task 1：统一工具声明与执行链（最高优先级）

**预计文件：**新增 `cloud/go2/agentcore/tools/registry.py`、`executor.py`、`agentcore/llm.py`、`agentcore/rules.py`；修改 `tools.py`、`agent.py`、`soul.py`、`memory/daily_summary.py`，必要时修改 `router.py` 和对应测试。

- [ ] 定义每个工具的唯一声明：名称、描述、参数 schema、可用角色、是否写入持久状态、是否运动、超时、返回值归一规则。先覆盖现有 12 个工具，不扩充能力。
- [ ] 将 `tools.py` 中的 `get_text_llm`、`get_vision_llm` 移到 `agentcore/llm.py`；将 `load_rules`、`save_rules`、`check_rules`、`record_rule_completion` 移到 `agentcore/rules.py`。工具实现仍留在 `tools/`，按运动/感知/导航等现有职责分组，避免把模型配置和规则存储误认为基础 Tool。
- [ ] 由声明生成 planner 可见的工具描述，并校验 `_SKILL_TO_TOOL` 的目标；未知 `skill_id` 明确返回错误，不让它进入未知工具执行分支。
- [ ] 执行流程固定为：查工具 → 校验参数类型与必填项 → 检查 `ExecutionContext` 权限 → 检查设备连接、运动租约及所需 FSM 条件 → 限时调用 → 将结果归一为结构化状态 → 记录审计信息。底层 `require_admin`、`require_persist` 和设备约束继续作为最终防线。
- [ ] 普通对话和 SSE 共用此执行函数；`reactive`、`free_explore` 两个模式大脑直接触发的运动工具也经过相同的运动前置条件和结果归一。设备页 FSM 按钮与摇杆仍走现有命令入口，但复用同一底层运动校验与租约约束；摇杆不进入 LLM 工具规划。保持动作串行，不为运动工具开启并发。
- [ ] 在服务端硬限制最多 3 次工具调用。模型输出 JSON 无法解析、格式错误、调用超限和合法的空列表分别处理，避免解析失败被当作「无需工具」。
- [ ] 把 `go2_sport` 等返回的「已接收」改为结构化 `accepted`；回复、`actions_taken` 和 SSE `tool_done` 都依据同一状态生成。`accepted` 不能视为完成，不自动推进依赖前一步完成的运动；允许显式停止与状态查询。
- [ ] `go2_move` 同时检查移动指令和停止指令的返回状态；参数约束覆盖方向、速度有限值及范围、时长范围。停止失败或无法确认时报告未确认状态，并保留可追踪的任务标识。
- [ ] 定义 `needs_clarification` 与错误参数的区别：自然语言目标缺少必要信息时不调用工具，返回问题和缺失字段；明确参数不合法时返回校验错误。设备页与主聊天两条入口都保留按会话、设备关联且有过期时间的待澄清目标；后一轮补齐后重新校验，切换话题不沿用旧目标。

**验收：**错误参数或权限不足时未下发 WebRTC 命令；未知工具、未知 skill 与无效 JSON 有明确错误；`accepted` 不被报告为已完成或继续依赖运动；停止失败会出现在结果中；「移动两步」只澄清、不发运动命令；同一模拟指令在设备页、主聊天委派和 SSE 路径得到一致的最终状态。运行 `test_agent.py`、`test_access_control.py`、`test_router.py`，补跑 API 与编排器相关测试。

### Task 1A：对齐端到端超时与长任务生命周期

**预计文件：**`cloud/api/intents.py`、`cloud/orchestrator/graph.py`、`cloud/orchestrator/tools.py`、`cloud/go2/router.py`、`cloud/go2/agent.py`、`app/src/CommandBus.js`，以及相应测试；按入口逐步调整。

- [ ] 给规划、每次工具调用、回复和 HTTP 传输设同一条可计算的任务预算；设备页、主聊天与 Go2 服务端使用一致的任务标识和截止时间，不能在外层 10 秒超时后继续把内层 20 秒动作当作已失败。
- [ ] 同步窗口内能完成的请求返回最终结果；预计超过窗口的请求返回 HTTP 202、`result: executing` 与 `task_id`，并提供按任务标识查询/接收最终状态的途径。`executing` 表示服务端任务仍在运行，`accepted` 专指设备只确认收到动作命令；PWA 和编排器以最终状态更新回复。
- [ ] 客户端等待超时与服务端任务超时分开处理；客户端超时后可查询原任务状态或显式取消，重试不得隐式重复下发运动。取消运动须走设备停止/确认流程；如果停止无法确认，结果保持未确认。
- [ ] 用模拟的慢 LLM、20 秒动作和连续工具验证两条入口的状态流转；测试使用模拟时间或短超时配置，保持定向测试快速完成。

**验收：**同一个 `task_id` 可串起接收、执行和最终结果；设备页与主聊天不会先报超时、随后悄悄完成；取消与重试不会重复运动；同步/异步路径均区分 `accepted` 和 `ok`。

### Task 2：为记忆召回设置选择规则与预算

**预计文件：**新增 `cloud/go2/agentcore/memory/recall.py`；修改 `agent.py`、`episode.py`、`daily_summary.py` 与记忆测试。

- [ ] 定义召回输入：问题、会话身份、可用字符预算；输出为带来源与时间范围的记忆片段。访客只读本会话历史与允许的即时状态，管理员可读 Go2 的持久情节与日摘要。
- [ ] 对「今天做了什么」「某天发生什么」「去过哪里」「当前位置」等问题先按时间和事件类型筛选，再在候选中做轻量相关性选择；普通动作指令只附上少量必要近期事件。
- [ ] 为当天事件和近日日摘要分别设条数与总字符上限；超预算时优先保留最近且与问题相关的条目。保留 `episode.py` 原始 JSONL，摘要仍由 `daily_summary.py` 生成。
- [ ] 摘要记录来源日期，并检查跨天多日未运行后补摘要的行为；补写时受已有事件保留期和单次任务预算限制。
- [ ] 对记忆片段标记为历史数据，不把其中的用户原话或视觉文本当作运行指令。

**验收：**当天有大量事件时，进入 planner 的记忆长度仍不超过设定预算；问指定日期可召回对应摘要；访客 A 不看到访客 B 的会话；无记忆时明确回答未知。运行 `test_episode_memory.py`、`test_daily_summary.py`、`test_agent.py`。

### Task 3：按「服务 / Tool / Mode / Skill / Agent Card skill」归位现有代码

**预计文件：**将 `agentcore/skills/vision.py`、`identity.py` 归入 `agentcore/perception/`；将 `agentcore/skills/reactive.py`、`navigation/drive.py` 中的两个模式大脑归入 `agentcore/mode/reactive.py`、`agentcore/mode/free_explore.py`；新增 `cloud/go2/card.py`（含 Card skill 白名单或引用独立声明表）；修改导入、`agent.py`、`router.py` 和相关测试。分小步迁移，保持每一步可导入、可运行。

- [ ] 先把视觉检测和身份状态明确为 `perception/` 底层服务，更新引用它们的导入；导航器、记忆和设备连接继续留在各自的领域目录。只改归属和名称，不改识别阈值与行为。
- [ ] 建立 `agentcore/mode/`：迁移 `ReactiveMind` 为 `reactive` 模式大脑，迁移 `Drive` 为 `free_explore` 模式大脑；它们保留各自的触发、状态、LLM 决策和生命周期。`manual` 只关闭自主决策；`router.py` 的模式切换继续统一管理启停。导航路径规划和运动闭环继续留在 `navigation/`。
- [ ] 把两个模式大脑中有物理副作用的动作接到 Task 1 的 tool executor；视觉帧、身份快照、记忆仍作为输入数据。用户中断、模式切换和设备断开都能终止后续动作。`agentcore/skills/` 本期不放这两个模式，也不为占位保留空目录。
- [ ] 建立显式的 Card skill 白名单，引用 Task 1 的 tool 声明，不复制参数定义。保留 4 个现有 ID：`go2_chat → Planner`，`go2_sport → go2_sport`，`go2_navigate → go2_navigate_to`，`go2_recognize_person → go2_recognize_person`；运动校验完成后增加一个 `go2_move → go2_move`，以方向、速度、时长为参数。Agent Card 从白名单生成 `skills`；收到 `skill_id` 时从同处查入口并校验参数。两个自主 Mode 不自动进入 Card。
- [ ] 将 `router.py` 中的 Card 构建与 MQTT 控制面发布抽入 `card.py`，路由只负责 HTTP 请求和模式/连接装配；保持 retained card/status 的原有发布时机。暂不为了目录对称拆分全部 HTTP 端点。
- [ ] 用 Task 0 的运动样例核对编排器能否选择 `go2_move`，以及 Go2 能否把「前进 2 秒」转成受控移动；对「移动两步」不能精确执行时必须澄清。
- [ ] 自然语言 Planner 和两个 Mode 大脑可选择内部 tools。以后若某个用户目标确实需要可独立调用、可复用的多 tool 流程，再把它加入内部 Skill 层，并决定是否在 Card 公布。

**验收：**`mode/` 分别承载 `ReactiveMind` 与 `Drive`，没有把模式伪装成 Skill；视觉与身份服务在三种模式下继续工作；两个模式的物理动作都经过统一 tool executor；Card 条目按覆盖检查决定，内部 tool 和自主模式不会自动出现在 Card；短距离移动有稳定入口且「两步」不会被谎报为精确完成；修改公开参数或删除对应工具时，一致性测试会发现漂移。

### Task 4：集中管理 prompt 的稳定规则与动态上下文

**预计文件：**新增 `cloud/go2/agentcore/prompts.py` 或 `prompts/`；修改 `agent.py`、`soul.py`、`mode/reactive.py`、`mode/free_explore.py`、`memory/daily_summary.py` 和 prompt 测试。

- [ ] 建立统一的 prompt 组装入口，区分固定约束（权限、工具使用、动作结果语义）、性格表达、当前任务说明、设备实时状态、选中记忆与工具结果。
- [ ] 固定约束由代码版本管理；`soul.py` 的自动演化只更新性格表达段，不能覆盖安全约束或工具 schema。
- [ ] 对话 planner、最终回复、`reactive` 模式大脑、`free_explore` 模式大脑、日摘要分别使用任务模板；动态字段按明确顺序注入，并给视觉描述、事件、用户原话标识其数据来源。
- [ ] 为调试记录模板版本、各段长度、选用的记忆来源、工具声明版本和 LLM 错误码；不记录密钥、人脸图、完整敏感对话。
- [ ] 建立可渲染的固定样例，用于检查 prompt 中安全段存在、动态数据不会覆盖安全段、长度超预算时按预定顺序裁剪。

**验收：**设备页、主聊天委派、SSE 对话及两个 Mode 大脑都能明确说明其 prompt 版本与来源；修改性格后工具权限和动作语义不变；同一输入的组装顺序稳定；planner 与回复使用相同的设备能力定义。

## 4. 跨任务验收与发布

1. 每个 Task 完成后运行该 Task 的定向测试，再运行 `uv run python -m pytest cloud/go2/tests -q`。涉及 Card、双入口或超时时，补跑 `cloud/api/tests`、`cloud/orchestrator/tests` 中相关测试。
2. 用模拟设备覆盖动作完成、只确认接收、被 FSM 或租约拒绝、停止失败、请求澄清；检查设备页 REST、主聊天编排、SSE、最终回复和审计中的状态一致。
3. 实机验收按「状态查询 → 原地姿态动作 → 短距离移动 → 导航」顺序进行；每次确认停止命令与动作状态，异常时停在当前步骤。
4. 检查 `manual / reactive / free_explore` 三种模式：各模式按预期切换；`ReactiveMind` 和 `Drive` 保持独立决策，自主动作不会绕过运动校验，用户中断仍能终止相应任务。
5. 保持旧 Agent Card `skill_id` 和 `/api/go2/chat`、`/api/go2/chat/stream` 的外部接口；若确需改变响应字段，先增加兼容字段并更新编排器与 PWA 消费方。

## 5. 审阅时需要确认的设计选择

| 议题 | 计划采用的默认方案 | 调整点 |
|---|---|---|
| Mode 与 Skill | `reactive`、`free_explore` 迁入 `agentcore/mode/`，保留两个独立大脑；本期不强行新建内部 Skill | 出现新的、可独立调用且可复用的跨工具目标后，再新增 Skill。 |
| 记忆召回 | 先用时间、事件类型和关键词筛选，设置硬预算 | 若真实对话样例显示召回质量不足，再评估模型辅助选择或向量检索。 |
| 性格演化 | 保留自动演化，但只允许更改性格表达段 | 可改为人工审核后启用。 |
| 动作完成语义 | 设备确认完成为 `ok`；发送成功但无完成反馈为 `accepted` | 需核对各 WebRTC 动作实际提供的反馈范围。 |
| 请求澄清 | 缺少必要信息且尚未执行时返回 `needs_clarification`，由两条聊天入口继续同一会话 | 不支持的精确步数要说明能力边界，不能靠补充参数假装支持。 |
| 长任务反馈 | 超过同步窗口时返回 HTTP 202、`executing + task_id`，两条入口按任务标识获取最终结果 | 接口设计时以实际动作和 LLM 耗时测量为准。 |

## 6. 本期边界

本计划不引入通用 MCP 平台、Shell 型技能、跨设备记忆同步或新的模型服务；不改 ESP32 三层结构，也不重做 PWA 页面。只有当任务 0 的样例或后续验收证明这些能力必要时，再单独设计。
