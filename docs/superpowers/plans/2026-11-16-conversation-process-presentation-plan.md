# SSM 对话、活动与 Graph 过程展示改造计划

- 日期：2026-11-16
- 状态：待实施。本次完成相关调用链、消息出口和展示代码审查，本文不是实施完成或测试通过报告。
- 目标：聊天保留角色真实的人格发言；可展开内容展示实际依据、调用和结果；删除重复转述、空泛阶段文案及替代后失去用途的旧代码。
- 范围：管家、灯、Go2，普通聊天、委托任务、自主活动、搜索、教导、同伴通信和游戏，以及 MQTT/HTTP/SSE、活动持久化和 PWA 展示。
- 执行顺序：先统一事实与回复职责，再接通传输和历史，最后替换展示并删除旧实现。各阶段完成局部验证，不保留新旧两套运行分支。

## 一、完成后的体验

1. 聊天气泡不显示设备形象。角色名称只在需要辨别说话者时出现；同一角色连续执行的多个步骤不逐行重复名称。设备页和活动成员区仍可使用形象，真实回复的情绪继续有效。
2. 普通聊天直接展示原角色模型的回复，没有空的过程面板，也没有“上下文／你／原问题”的重复块。
3. 有实际调用时，使用具体动作或原模型给出的简短公开说明作为展开入口；去掉“处理过程”“总管家处理请求”“总管家理解请求”“整理上下文”等占位叙述。
4. 展开后能看到：什么事实触发了选择、调用了谁的什么能力、传了哪些相关参数、收到了什么结果、是否据此继续或停止。缺失的依据不补写；仅有调用和回执也可形成有效详情。
5. 失败、部分完成、结果未确认和待用户补充保持可见。成功详情默认收起；正在执行的具体调用保持可见；异常默认展开其原因，不展开整段无关历史。
6. 活动结束只保留结束标签及有意义的结果，不再显示“最新动态：本次活动已经结束”。详情入口不显示“活动记录 4”等计数。
7. 用户、设备和同伴的真实发言保留人格、语气、情绪；不额外生成一段“它刚才做了什么”的旁白。

“没有固定套话”不等于删除状态、错误、字段名和无障碍标签。设备离线、请求被拒绝、尚未确认等事实仍需明确表达；不能将错误藏进空白面板，也不能把系统状态冒充角色发言。

## 二、审查结论与实际链路

### 2.1 主链路

```text
用户请求
  → ApiClient / CommandBus
  → API 认证、可信上下文
  → 管家 MQTT 意图入口，或灯 / Go2 直接聊天入口
  → 对应角色的人格与业务提示词
  → common/runner.py：model → tool → model / ask / finalize
      model：原生模型消息、tool_calls、当前证据
      tool：权限与参数校验 → 动作账本 → MQTT / HTTP → 真实回执
      ask：LangGraph interrupt → 同一运行恢复
      finalize：复用最终发言；没有最终发言时根据真实结果收尾
  → 公开事实投影 + 唯一回复出口
  → MQTT feedback / API publish_agent_event
  → SSE；活动内容同时进入既有活动存储
  → 前端按运行、步骤和事件身份合并
  → 人格气泡 + 可展开的实际调用与依据
```

自主调光、Go2 自主仲裁/搜索、教导和游戏还有各自的既有 Graph。它们不能靠修改主 `runner.py` 自动覆盖，需要分别接入同一公开事实契约，但不合并成一个新运行时。

### 2.2 目前混在一起的四类内容

| 内容 | 当前来源 | 处理 |
|---|---|---|
| 生命周期信号 | planning、considering、dispatching、observed、summarizing 等回调 | 保留忙碌、超时和结束控制需要的信号；不把每个信号翻译成一句聊天记录 |
| 真实运行事实 | 原生 tool call、执行账本、设备回执、观察、活动成员结果 | 补齐关联和公开字段，作为详情主体 |
| 人格发言 | 原角色最终回复、同伴 content、游戏 speech、自主结果回复 | 保留，明确谁负责生成与发送，复用已有输出 |
| 重复解说 | `common/progress.py` 的额外模型、前端阶段模板、部分游戏脚本串词 | 删除生成和展示路径，不只隐藏 UI |

### 2.3 为什么已有的人格提示词之外还有回复 Graph

三个主角色的决策提示词已经包含人格。当前共享运行器还规定：工具调用轮不输出用户可见文字；工具结果 `final=true` 后不再继续规划。因此，一些任务结束时只有回执、没有最终发言。此时需要首次结果回复，但不需要独立的“人格包装 Graph”。

- `common/replies.py::finalize_reply` 已优先复用 `decision_draft`；没有最终发言且存在动作结果时才调用模型收尾。这项职责保留在原 Graph 的 `finalize/reply` 节点内。
- `go2/workflows/reply.py` 是单节点的独立回复 Graph，由 `go2/mode/free_explore.py` 在观察、运动结果出来后调用。把这项职责移入既有自主 Graph 的结果分支后删除此文件，不把导航器本身改成模型循环。
- `lamp/workflows/physical_reply.py` 是 observe → compose → publish 的独立 Graph。目前仓库内只找到内部 `/agent/replies` 接口及测试引用，没有找到生产调用方；现有管家已有 `steward/device_replies.py` 的汇总回复出口。删除这一遗留 Graph、请求模型和接口，保留当前实际使用的回执与回复路径。
- `common/progress.py` 在每步之后额外调用模型生成进展转述，和上述“首次最终回复”不同。直接删除，不迁移到另一个 Graph 或后台线程。

人格贯穿决策与表达。减少重复调用不能变成“执行前先写成功回复”，也不能将必要的结果回复替换成固定模板。

### 2.4 涉及文件与问题清单

以下为本次审查覆盖的相关模块；实施时以当时工作区版本复核符号，不覆盖并行修改。

| 层 / 文件 | 已发现的问题或职责 | 实施要求 |
|---|---|---|
| `cloud/agents/common/runner.py` | model/tool/ask/finalize 已具备真实边界；外部进展只暴露薄层阶段 | 从真实决策、步骤、回执投影事件，保留串行、终结、中断和恢复语义 |
| `cloud/agents/common/messages.py`、`conversation.py` | 已使用 System/Human/AI tool call/Tool 消息；对话历史承载用户和最终回复 | 不把 UI 套话写回历史；不公开系统提示词和上下文全量 |
| `cloud/agents/common/replies.py`、`outcomes.py` | 复用最终发言，处理真实结果和已恢复失败 | 保留公共收尾函数；展示有效结果及恢复关系，不把恢复前失败当最终状态 |
| `cloud/agents/common/progress.py` | 额外模型、后台线程、进展转述 | 整个删除，清理调用、线程关闭标记和专属测试 |
| `cloud/agents/common/progress_facts.py` | 只保留少量参数、reason 和 result_state；缺少轮次、证据及结构化业务结果 | 原地改成统一有界公开投影，不另建一套并行序列化器 |
| `cloud/agents/{steward,lamp,go2}/graph.py` | 各自拼回调、额外进展解说；角色和委托回复存在多个出口 | 移除解说；统一事实回调，明确回复归属 |
| `cloud/agents/{steward,lamp,go2}/prompts.py` | 人格已有；公开解释和最终发言尚未与过程事实明确区分 | 在同次决策提供可选简短说明，不另起解释模型，不强迫闲聊解释自己 |
| `cloud/agents/steward/tools/__init__.py` | 查询、活动、配置、教导、委托结果不同；start_activity 外层成功可含成员部分失败 | 按结果类型投影，不能仅看外层 ok 显示全部成功 |
| `cloud/agents/steward/device_replies.py`、`replies.py` | 已按设备汇总结果并尝试复用子回复；管家又生成总答复 | 保留单一设备发言出口；复用子回复，管家仅补充跨设备结论和需用户处理的事项 |
| `cloud/agents/lamp/workflows/reactive.py`、`runtime.py` | 自主 Graph 有实际 plan/act/reply；进展动作文本是模板；执行结果可有 decision_basis 但发布不完整 | 传递真实决策与当时证据，保留原 reply 节点，删除模板过程叙述 |
| `cloud/agents/lamp/tools/execute.py`、`schemas.py` | 已有 decision_reason/evidence_refs 和证据校验 | 复用现有契约，避免重复字段；游戏必需证据仍严格校验 |
| `cloud/agents/lamp/workflows/physical_reply.py`、`cloud/esp32/router.py` | 遗留独立回复 Graph 和内部接口 | 删除 Graph、PhysicalReplyRequest、reply_to_physical_task 及仅服务该接口的依赖 |
| `cloud/agents/go2/workflows/free_explore.py`、`cloud/go2/mode/free_explore.py` | 仲裁 Graph 和实际导航/观察调度分离；调度器另起回复 Graph | 以原任务/观察 ID 把结果送回既有自主 Graph 的回复分支；不重新执行仲裁或导航 |
| `cloud/agents/go2/workflows/reply.py` | 独立单节点回复 Graph、独立线程标识 | 迁移回复职责及幂等后删除 |
| `cloud/agents/go2/workflows/search.py`、`search_progress.py` | 搜索有真实画面、候选和导航结果，也有大段阶段模板 | 保留真实观察与执行器判断，删除假装模型思考的模板 |
| `cloud/go2/navigation/`、`perception/`、`tools/`、`tasks.py` | 定位、停止、视觉复核、导航结果等真实来源 | 只补关联与公开投影，不改运动安全、观测判定和任务执行算法 |
| `cloud/agents/common/training/workflow.py`、`training/tools.py` | 逐步执行与版本权限复核已有，保存/加载/动作不同 | 公开教学名称、ID/版本、当前步骤和真实回执，保持三种结果分离 |
| `cloud/agents/lamp/workflows/runtime.py`、`cloud/go2/service.py`、`training_events.py` | 两种设备的教导适配、触发和 before/after step 出口 | 接入同一事实契约，保留跳过已达成目标、暂停和迟到回执语义 |
| `cloud/agents/go2/workflows/social.py`、`cloud/peers/messaging.py`、`gateway.py` | 模型 content/reason 分开；发送与接收都可能写活动；消息多阶段回执 | 发言只保存一次，收送状态另行更新，不把送达视为设备动作成功 |
| `cloud/peers/games/{color_hunt,rps_siege,push_delivery}.py` 及决策模块 | 模型 speech 混有规则播报、阶段串词和固定第一人称行动文案 | speech 原样保留；规则、回合、结果转成结构化事实；删除额外旁白，不提前泄露未公开出拳 |
| `cloud/orchestrator/main.py`、`tools.py` | 初始 planning ACK、typed feedback 和旧阶段反馈并存 | 控制状态与可见内容分开；统一事实出口后删除仅为旧展示服务的路径 |
| `cloud/api/intents.py` | MQTT 转 SSE 白名单遗漏 decision_basis 等；事件身份、权限与 scope 在此构造 | 更新显式白名单与稳定身份，同源可信规则不变 |
| `cloud/space/activity.py`、`decisions.py`、`cloud/api/activity.py` | 历史有界保存，证据和 state_at_event 有独立处理 | 实时与历史采用同一公开投影，保留原始时间、版本和证据有效性 |
| `cloud/observability/`、`scripts/trace_task.py` | 日志可核实模型调用；latest 会话筛选仍依赖 ses_ 前缀 | 日志不直接给浏览器；修正对真实 chat_ 会话的 latest 选择，用 fixture 验证 |
| `app/src/app.jsx`、`pages/FsmDevicePage.jsx` | 插入“已收到请求”、靠 processEventText 补叙述；首页部分路径将 target 当 actor | 原事件决定内容，agent_id 决定执行者，target_unit_id 只表示目标 |
| `app/src/CommandBus.js` | accepted 被一概描述为未完成，部分等待也将 accepted 继续轮询 | 区分 dispatch_only 的提交确认与真实动作成功，不借 UI 改写后端 accepted/ok 契约 |
| `app/src/ApiClient.js`、`hooks/useSendIntent.js` | SSE 旧阶段监听同时承担超时、追问、完成控制 | 移除旧展示前保留对应运行控制；空内容事件也能更新 busy/终结 |
| `app/src/utils/agentMeta.js`、`runProcess.js` | 阶段模板、字段丢失、按 run/unit 粗合并和浅覆盖 | 删除模板函数；按明确身份合并事实，不用空值覆盖已知事实 |
| `app/src/components/RunProcess.jsx`、`ChatMessages.jsx` | “处理过程”、重复角色、空步骤和粗粒度分组 | 单一组件渲染事实，连续角色归组，保留有意义的详情 |
| `app/src/components/ActivityFeed.jsx`、`utils/activityView.js` | 活动阶段模板、过程附着与依据展示；相邻记录容易被当因果 | 只按显式父子/触发关系关联；生命周期标签和详情去重 |
| `app/src/hooks/useActivityTimeline.js`、`useSpaceActivity.js` | REST/SSE 合并、窗口裁剪、生命周期同步；传感器摘要是实际数据 | 保留数据事实和过期判断；去重不按文本，不复活已结束活动 |
| `app/src/components/ChatPanel.jsx`、`avatars/`、`styles/`、`app/index.html` | 加载反馈、设备表情及脚本装配受影响 | 去掉气泡形象与死样式；保留必要 busy/aria 信号、成员形象及加载顺序 |

## 三、公开事实契约

### 3.1 沿用原 Graph 和事件通道

使用当前 LangGraph 节点、原生消息、检查点和步骤账本作为来源。主运行器在 model/tool/ask/finalize 的现有回调边界构造公开投影；专用 Graph 在对应节点边界调用同一投影函数。无需为了展示把所有 `ainvoke` 改成另一套执行循环。

如需要流式节点更新，使用当前依赖支持的 LangGraph `updates/custom` 通道接到同一投影入口，不向浏览器直接发送 `values/debug/checkpoints` 原始内容，也不同时用两个来源重复发布同一事实。当前锁文件 LangGraph 为 1.2.18；本次不需要升级依赖。

MQTT/HTTP 远程设备不是本地 LangGraph 子图，必须保留显式 parent_run_id/step_id 关联，不能靠 subgraphs 开关推断远程因果。

### 3.2 最小字段及来源

继续使用 `agent_progress`、`agent_reply`、`run_stage` 等现有事件类型；扩展其受控字段，不另建事件数据库。以下新字段名为实施目标，现有同义字段应统一后删除重复字段。

| 信息 | 字段 / 来源 | 规则 |
|---|---|---|
| 身份 | event_id、source_event_id、trace_id、run_id、parent_run_id、step_id、task_id | 来自可信运行/执行层；投影和重放保持相同身份 |
| 顺序 | sequence、model_turn、ts | 运行内明确递增；不能仅用浏览器到达顺序覆盖结果 |
| 角色与目标 | agent_id、target_unit_id | 前者是谁执行/说话，后者是谁被调用；不能互换 |
| 信息类型 | fact_kind | observation / decision / call / result / question；生命周期继续用 stage，不生成一条叙述 |
| 调用 | tool、skill_id、capability_name、arguments | 名称来自现有工具/Card；只公开允许的业务参数，不重复维护能力目录 |
| 公开说明 | decision_basis.reason、evidence | 同次原决策的简短说明及服务端校验后的证据快照；不是私有推理全文 |
| 观察 | observation_event_id、观察摘要、frame_id/现有媒体引用、as_of | 原传感器/视觉输出；保留新鲜度，未知不能变成否定或肯定 |
| 回执 | status、result_kind、结构化公开结果、错误码/原因 | 区分 action/query/message/activity/configuration/training；外层 ok 不覆盖内层失败 |
| 因果 | trigger_message_id、reply_to、conversation_id、证据 ref | 只使用显式引用，不用相邻时间猜测 |
| 教导 | teaching_id、version、name、step_number | 从已解析且校验过的教导生成，不让模型或浏览器声明已加载/已完成 |
| 发言 | text、emotion、reply_kind、回复来源身份 | 原模型发言；系统错误另有类型，不假冒人格输出 |

扩展 `progress_facts.py` 的逐类型白名单：活动只投影成员加入/失败/停止确认；教导只投影名称版本和步骤；搜索只投影实际观察、候选选择和结果；同伴只投影公开消息身份及送达状态。控制递归深度、条目数量和字符串长度；不透传任意 result/context/memory 字典。

### 3.3 公开解释怎样产生

1. 复用已存在的 `decision_reason/evidence_refs`。共享工具 schema 按现有 `final` 元数据机制补充可选公开说明字段；已有同名声明只保留一个定义，不添加第二套解析器。
2. 说明在原决策模型调用中产生，要求只解释本次可公开的选择及依据。没有必要说明、仅有普通聊天或原生工具调用无说明时允许留空。
3. 从业务参数中分离展示元数据再做动作账本指纹和派发，防止同一动作因解释文字不同而变成新动作。需要决策证据的既有执行校验通过明确元数据路径读取，不能因剥离而绕过游戏安全约束。
4. 服务端用本次决策实际拿到的 decision_space 解析 evidence_refs，保存当时观察的来源、时间与摘要；不能拿执行后的新状态冒充决策依据。
5. 可选说明格式无效时丢弃说明并记录诊断，不凭它阻断普通有效动作；现有 require_observation 等强制证据要求仍然必须满足。
6. 前端不再从 stage、工具名或相邻消息生成“模型为什么这样做”。执行器给出的停止/安全原因标明为执行反馈，不冒充模型理由。

### 3.4 合并、历史和结果语义

- 同一 call 的 planned/executing/result 更新一个步骤；不同 tool_call_id 保持独立，即使工具名和参数相同。
- 顺序执行以实际开始与回执为准；模型返回的待执行调用不能提前显示为已经执行。失败清空批次时，剩余调用不显示成功。
- 重复 SSE、HTTP 返回、历史补载按源事件/回复身份合并；文本一样的独立发言不能合并。
- late result 只能更新原 task/step，不覆盖新运行，不重新派发动作。终态对陈旧阶段有优先级，但允许合法的同任务迟到回执补全未知状态。
- `accepted` 与 `ok` 按现有业务契约处理为动作成功；`dispatch_only` 表示仅提交成功；身体姿态仍只认真实遥测。消息 accepted 和活动部分加入分别按各自 result_kind 展示。
- 历史只显示当时存下来的事实。缺证据就没有证据区，不重跑模型补说明，不改写现有运行资产。旧 narrated 记录不再渲染为新的过程说明；这是读取历史时的一次过滤，不是保留旧生成链路。

## 四、人格回复归属与 Graph 收敛

| 场景 | 生成/复用规则 | 发送职责 |
|---|---|---|
| 管家或设备普通聊天 | 直接复用原决策最终 text/emotion | 原角色现有会话出口 |
| 工具结束后已有合法最终回复 | 直接复用，不再改写 | 原角色 finalize |
| 工具直接终结，只有真实回执 | 在原 Graph finalize/reply 节点生成一次首次结果回复，禁止再次调用业务工具 | 原角色唯一出口 |
| 管家委托设备 | 子 Graph 已给回复时复用；纯原子工具没有设备发言时按真实设备结果汇总生成一次 | 现有 device_replies 负责协调轮的设备发言去重；不得与子 Graph 双发 |
| 同设备多次委托 | 收集整个协调轮的结果；已有完整最终回复则复用，否则只生成一次设备汇总 | 不逐动作生成设备旁白再生成整段汇总 |
| 灯自主调光 | 原 reactive Graph 的 reply 节点，保持现有有效性和回复门控 | 原活动出口 |
| Go2 自主观察/导航 | 结果送回既有自主 Graph 的 reply 分支；已有对此事件的发言则跳过 | 原活动出口，复用任务/观察身份 |
| 同伴交流和游戏 | 原决策 content/speech 直接使用；reason/evidence 只进详情 | 原消息发送出口，显式发信与自动最终回复共用去重身份 |
| 系统拒绝、离线、回复生成失败 | 保留真实错误或状态，不能宣称角色已经说过 | 系统反馈，不伪造成设备人格消息 |

Go2 收敛的具体方式：在 `MotivationState` 增加有界的结果输入和已回复身份；既有 `build_motivation_graph` 入口根据“结果输入”直接路由到 reply，然后结束，不经过 advance 再次仲裁。输入携带原任务/观察 ID、真实结果和 generation/activity 所有权。复用公共 `finalize_reply` 函数、角色人格、现有检查点与发言门控；发送前再次校验活动和用户中断。迁移后删除 `workflows/reply.py` 和原独立线程标识。该分支不新增 Agent 循环，不移动导航安全逻辑。

管家最终回复不机械重复设备的整句话；有跨设备部分失败、待用户决定或整体结果时简短说明。普通聊天仍允许正常人格表达，不把所有回复统一压成固定字数模板。

## 五、前端信息排布

### 默认视图

保留用户原话和角色发言。具体进展入口优先展示当前实际调用的能力名称与必要目标；若本轮有可公开的原决策说明，可直接使用该说明。已经展示在气泡里的同一 source reply 不再复制成过程摘要。

运行还没有产生实际内容时只保留轻量 busy 状态，不创建“理解请求”等历史条目。无详情时没有空的展开按钮。展开按钮仍提供 aria-label、aria-expanded 和键盘操作，不需要在视觉上显示“处理过程”。

### 展开视图

按真实顺序展示实际观察 → 可选决策说明及证据 → 调用目标/参数 → 回执 → 后续调用。只渲染存在的部分，不给每一步凑齐模板栏目。连续同角色标一次名字，角色切换再标；跨设备关系依赖明确 parent/step 引用。

照片、定位摘要和参数使用现有组件和 Card 字段格式化。目标、色值、亮度等确有信息的内容可以显示，不能为了“自然语言”再调用模型改写，也不能让原始 JSON 占据聊天正文。

### 活动卡

标题、生命周期标签、成员真实状态、人格交流、重要观察/动作结果组成主内容。结束卡不再添加固定“最新动态”，活动详情不显示记录数。成员离线、加入失败、停止未确认等不能因为卡片标记已结束而被隐藏。

同伴消息与回执分开：消息气泡只显示 content/speech；送达、接受、开始、最终结果更新对应消息或任务事实。游戏规则作为公开规则详情，不能拼成角色气泡的固定前缀。观察和行动只有显式 trigger/ref 才显示因果连线。

### 回归基线示例

针对本次审查的“我要干活了，你们自己玩吧”请求：实际调用是 start_activity，灯加入 reactive，Go2 因离线未加入，工具外层成功但活动部分完成。界面应直接保留原管家回复；展开可见这次活动调用与两个成员各自结果。不能凭阶段补出“总管家理解请求”，不能把 start_activity 的 ok 渲染为两个设备都已开始活动。

将这一场景制作成脱敏 fixture。示例不是要求模型固定输出某句话，也不是已通过新实现的验收结果。

## 六、实施步骤与交付条件

### P0：锁定当前基线与关联场景

- [ ] 记录工作区差异，保留用户及并行任务已有修改；尤其不覆盖 `models.py`、`conversation.py`、`runner.py` 的无关改动。
- [ ] 用脱敏 fixture 固定普通聊天、活动部分加入、两步串行、设备委托、搜索、教导、同伴游戏七类链路。
- [ ] 为每类明确原始消息/回执、运行与步骤身份、最终回复来源、当前多余模型调用次数。
- [ ] 复核 physical_reply 的调用引用；当前结论为无生产调用，按 P3 删除。若实施期间出现新调用，先迁入现有实际回复路径，不保留旧接口兜底。

交付：可重复的输入/事件样本及调用关系；不读取密钥，不复制人员/记忆/教导运行数据进 fixture。

### P1：统一事实投影与同次决策说明

- [ ] 原地扩展 `progress_facts.py` 的白名单、类型和关联字段，使用 Pydantic/现有 schema 验证公开元数据。
- [ ] 在 `runner.py` 的原节点边界记录 model_turn、tool_call_id、调用顺序、真实回执和恢复关联。
- [ ] 统一角色提示词的可选公开说明；复用 `space/decisions.py` 和原决策上下文。
- [ ] 补活动部分结果、教导步骤和观察的公开投影；保留 result_kind。
- [ ] 校验展示元数据不进入业务参数与动作去重指纹，不改变 final/accepted/interrupt 契约。

验证：tool_contract、sequential_tool、termination、human_input、outcome、decision、freshness 现有测试及必要新增断言。通过条件：每条展示事实能追溯来源，缺失说明不产生假说明，不新增解释模型调用。

### P2：移除进展解说，接入所有现有生产者

- [ ] 删除 `common/progress.py` 和三个 graph.py 的 submit_progress、narrated 回调与专属线程状态。
- [ ] 将灯 reactive、Go2 search、自主导航/观察的进展接入统一投影；移除 `_action_text` 和 search_progress 中纯阶段模板，保留真实参数、画面与结果。
- [ ] 接通灯执行器的 decision_basis，保留产生决策时的证据快照。
- [ ] 将教导 before/after step、同伴 message/status、游戏 action/result 接入同一事实字段。
- [ ] 游戏固定角色串词改为规则/回合/结果结构化事件；不新增模型为这些事实写旁白。

验证：lamp state_context/activity_reply/evidence、Go2 search/search_progress、training_dispatch、peer dialogue、三个游戏相关测试。通过条件：覆盖所有已列入口，没有用模板或新模型补回删除的解说。

### P3：收敛回复职责，删除独立回复 Graph

- [ ] 保留 common finalize_reply 的复用优先规则，测试普通聊天只有原决策回复。
- [ ] 明确 delegated 子运行的回复返回与发布职责；device_replies 对同一协调轮、设备只发送一次，保留原 emotion。
- [ ] 在既有 Go2 自主 Graph 增加结果直接进入 reply 的条件分支，迁移 observation/motion 回调、持久回复身份和门控。
- [ ] 结果分支不得重新执行 advance、工具或导航；发送前仍校验 generation、活动、连接和用户中断。
- [ ] 删除 Go2 独立 `workflows/reply.py`。
- [ ] 删除灯 `workflows/physical_reply.py`、内部回复接口、请求类及只服务它们的测试和导入。
- [ ] 清理“收尾失败也冒充角色说过固定句子”的路径，保留系统错误与实际业务结果。

验证：reply、device_replies、emotion、message、autonomous_visibility、activity_isolation、interrupted_run。通过条件：一次事实不会触发重复人格生成或重复发布；恢复不重发物理动作；普通聊天无新增调用，直接工具终结最多一次必要收尾调用（按实际发言角色计）。

### P4：打通 MQTT、SSE、历史与去重

- [ ] `api/intents.py` 的 MQTT 显式白名单接通公开说明、证据、轮次、父子关联和结构化结果；权限/scope 仍由服务端构造。
- [ ] API 直接事件、MQTT 事件和 `space/activity.py` 历史统一调用同一投影，保留原 as_of/ts。
- [ ] 源消息发送和接收写入活动使用同一消息身份；进度与最终回执有各自身份，不能被消息去重吞掉。
- [ ] 重放保持 source_event_id；按 sequence/终态合并，明确未知→迟到真实结果的更新规则。
- [ ] 修正 `CommandBus` 对 dispatch_only 和真实 accepted 的区分；保持原任务查询恢复，不重复下发。
- [ ] 将旧阶段协议中仍承担 busy/追问/完成的部分迁到现有类型事件后再删无消费者出口；列出仍保留的控制事件用途。

验证：api sse/chat_session、space activity_visibility/interaction_history、esp32 receipt_recovery、peer result_recovery、前端命令回归。通过条件：同一链路实时与刷新后含义相同，重连不重复气泡，不串会话、不串活动。

### P5：替换聊天和活动展示

- [ ] `app.jsx`、`FsmDevicePage.jsx` 去掉合成“已收到请求”等记录，空内容仍可更新运行控制状态。
- [ ] 修正 actor/target，`runProcess.js` 保留完整事实身份并采用逐字段合并，避免空值覆盖和跨步骤折叠。
- [ ] `RunProcess.jsx` 去掉固定标题、空步骤和重复角色，直接展示真实事实入口及有内容的展开区域。
- [ ] `ChatMessages.jsx` 按明确请求/运行关系分组；设备真实发言保持独立人格气泡。
- [ ] `ActivityFeed.jsx` 删除结束套话和计数；按显式引用连接消息、观察和行动，保留异常和停止未确认。
- [ ] 删除 `describeRunProgress`、`processEventText`、`processStepTitle` 和所有使用处；保留能力名称/真实参数格式化。
- [ ] 清理过期 CSS、气泡形象 hook、无用导入；确认脚本加载顺序、键盘展开、aria 状态和窄屏布局。

验证：check_chat_frontend、check_activity_frontend（含 --browser）、check_go2_frontend、check_go2_media_frontend、check_training_frontend、check_avatar_frontend、check_manual_control。通过条件：既符合第一节体验，也满足第八节数据正确性；只删文案的截图不能替代事件回放验证。

### P6：删除残余、文档同步与验收

- [ ] 按第七节清单全仓扫描旧符号与旧文案；删除专属废弃断言，不为通过旧测试恢复旧分支。
- [ ] 将仍有价值的回执、幂等、emotion 和断连测试迁到实际保留路径，不能整文件删除有效覆盖。
- [ ] `trace_task.py` latest 支持当前 chat_ 会话并排除测试样本，保持显式 ID 查询；用临时日志 fixture 验证。
- [ ] 同步 README、OBSERVABILITY、protocol/topics 的事件与回复职责说明；架构职责变化同步 AGENTS.md，保留其他并行更新。
- [ ] 完成相关回归后执行一次包含 *_checks.py 的全量云端回归，记录模拟测试与真实模型/硬件验收的界限。
- [ ] 发布时同步 API、编排器和前端版本；不为了本文或纯文档验证重启服务。代码实施后的重启依当时运行方式执行。

交付：一个有效实现，无旧生成链路、无备用模板叙述层、无独立人格包装 Graph；历史数据仍可只读展示已有事实。

## 七、明确删除清单

| 删除项 | 删除时机 / 替代职责 |
|---|---|
| `cloud/agents/common/progress.py` 全文件 | P2；真实事实投影替代，不提供转述替代品 |
| 三角色的 submit_progress、narrated 发布、专属关闭事件/线程导入 | P2；保留真实回调与运行生命周期 |
| `cloud/agents/go2/workflows/reply.py` 全文件及调用导入 | P3；既有自主 Graph 的 reply 分支负责首次结果表达 |
| `cloud/agents/lamp/workflows/physical_reply.py` 全文件 | P3；删除无生产调用入口，不迁移无用流程 |
| `PhysicalReplyRequest`、`reply_to_physical_task`、`/{unit_id}/agent/replies` | P3；清理专属导入、mock 与协议描述 |
| `describeRunProgress`、`processEventText`、`processStepTitle` | P5；统一事实模型直接驱动组件 |
| RunProcess 固定阶段标题、上下文重复块、无内容展开项 | P5；只渲染实际存在的内容 |
| ActivityFeed 仅为阶段补句子的函数/分支 | P5；保留真实状态和动作结果格式化 |
| reactive 的纯动作叙述模板、search_progress 的纯阶段叙述映射 | P2；保留或原地改写公开事实函数，不能顺手删掉真实观察字段 |
| 游戏的固定第一人称旁白、拼接在 speech 前的规则说明 | P2；规则和结果变为独立结构化事实，speech 不改写 |
| 废弃回归、submit_progress monkeypatch、physical_reply 专属测试 | 随对应代码删除；有效回执/幂等/人格断言迁入新路径 |
| 无引用 CSS、hook、脚本加载项、旧展示协议分支 | P5/P6；先确认无实际控制或其他页面消费者 |

不保留 deprecated wrapper、隐藏开关、第二版组件或新旧双发。保留公共 `replies.py`、原 Graph 收尾节点、真实错误处理、状态更新、Card 元数据和仍承担职责的测试。旧运行数据、教导、人员、记忆、人格和动作账本不是“多余旧代码”，不删除、不迁移覆盖。

## 八、验收矩阵

| 场景 | 必须看到 | 必须保证 |
|---|---|---|
| 普通闲聊 | 原角色人格回复 | 没有过程占位、没有额外润色模型 |
| 活动部分加入 | 各设备加入结果及真实失败原因 | 不把工具外层 ok 当全部加入成功 |
| 同角色两个串行调用 | 两个真实调用与各自回执，角色不过度重复 | 顺序正确，未执行步骤不显示成功 |
| 管家委托灯/Go2 | 各设备一次真实人格回复，相关调用可展开 | 子运行与管家不双发同一回复，不重复转述全文 |
| 纯物理调用 | 参数与真实回执，必要时一次结果回复 | 不要求额外决策模型；accepted 不伪造姿态 |
| Go2 观察/导航 | 实际画面/结果与对应发言 | 结果回复分支不重启仲裁或导航 |
| 搜索与视觉复核 | 观察时间、候选、动作及停止/复核结果 | 未找到/未知区分；规划器理由不冒充模型思考 |
| 教导 | 名称、版本、实际步骤和暂停/完成结果 | 保存、加载、物理完成不混同 |
| 同伴交流 | 原 content、reply_to 对应关系与真实状态 | send/receive 不重复气泡，送达不代表完成 |
| 三种游戏 | 人格 speech、公开规则、真实动作结果 | 无脚本旁白；保密选择直到允许公开才出现 |
| 用户补充信息 | 原 question 与恢复后的进展 | 同 run 恢复，不重新执行已完成动作 |
| 失败与迟到回执 | 异常默认可见，原步骤正确更新 | 不把迟到事件挂到新请求，不盲目重发 |
| SSE 重连 + HTTP 返回 + 历史补载 | 同一事实只出现一次 | 不以文本去重独立发言；旧事件不覆盖新状态 |
| 活动结束或用户中断 | 结束标签、必要结果/未确认原因 | 不显示结束套话、不复活活动、不发送过期人格回复 |
| 证据缺失/过期 | 不存在说明或明确显示原证据局限 | 不伪造当前事实、不替换原快照、不泄露提示词/密钥 |
| 窄屏和无障碍 | 可读的内容入口、正常展开与焦点 | 无气泡设备形象，其他形象情绪与 busy 不失效 |

额外检查模型调用数：移除全部 persona-progress 调用；已有最终回复不再调用收尾模型；一次自主结果最多生成一次需要的发言；生成失败不因 SSE/HTTP 重放再次触发生成。模型成本指标以真实日志或可计数 mock 为证，不凭 UI 行数推测。

## 九、验证命令与执行边界

以下为实施时执行，不表示本次文档工作已经运行或通过。工作目录为仓库根目录，pytest 使用临时目录/fixture；不得改写现有运行资产。

第一组，共享运行与角色回复：

```bash
UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest cloud/agents/common/tool_contract_checks.py cloud/agents/common/sequential_tool_checks.py cloud/agents/common/termination_checks.py cloud/agents/common/human_input_checks.py cloud/agents/common/message_checks.py cloud/agents/common/outcome_checks.py cloud/agents/common/emotion_checks.py cloud/agents/common/role_expression_checks.py cloud/agents/common/autonomous_visibility_checks.py cloud/agents/steward/reply_checks.py cloud/agents/steward/device_replies_checks.py -q
```

第二组，专用工作流与证据：

```bash
UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest cloud/agents/lamp/state_context_checks.py cloud/agents/lamp/activity_reply_checks.py cloud/agents/lamp/tools/evidence_checks.py cloud/agents/go2/tests cloud/agents/go2/search_progress_checks.py cloud/agents/go2/workflows/social_expression_checks.py cloud/agents/common/training_dispatch_checks.py cloud/agents/common/training/context_checks.py cloud/go2/training_event_checks.py cloud/space/decision_checks.py cloud/space/freshness_checks.py -q
```

第三组，传输、活动与同伴：

```bash
UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest cloud/api/sse_checks.py cloud/api/chat_session_checks.py cloud/api/activity_wakeup_checks.py cloud/space/activity_visibility_checks.py cloud/space/interaction_history_checks.py cloud/esp32/receipt_recovery_checks.py cloud/peers/dialogue_checks.py cloud/peers/result_recovery_checks.py cloud/peers/activity_isolation_checks.py cloud/peers/color_hunt_context_checks.py cloud/peers/games/rps_siege_checks.py cloud/peers/games/push_delivery_checks.py -q
```

浏览器环境按各脚本头部准备 `NODE_PATH`（sucrase/puppeteer）、`SSM_CHECK_REACT`、`SSM_CHECK_REACT_DOM` 和 `PUPPETEER_EXECUTABLE_PATH`。字体和截图路径按需设置，不把本机临时路径写进产品。模拟 API 不得连接真实设备。

```bash
node scripts/check_chat_frontend.cjs
node scripts/check_activity_frontend.cjs --browser
node scripts/check_go2_frontend.cjs
node scripts/check_go2_media_frontend.cjs
node scripts/check_training_frontend.cjs
node scripts/check_avatar_frontend.cjs
node scripts/check_manual_control.cjs
```

收尾检查：

```bash
UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest cloud -o 'python_files=test_*.py *_test.py *_checks.py' -q
git diff --check
rg -n 'submit_progress|progress_messages|run_autonomous_reply_graph|run_physical_reply|PhysicalReplyRequest|describeRunProgress|processEventText|processStepTitle' cloud app scripts --glob '!*.json'
rg -n '处理过程|理解请求|整理上下文|本次活动已经结束|活动记录' app/src
```

退役符号在产品代码中的命中应为零；文档和明确断言“不得再出现”的负向测试可以保留文字，逐条解释而非机械删除。现有混合测试文件保留仍有效部分。真实模型的自然度、实体手机体验、设备物理效果分别记录为后续验收，不以模拟结果代替。

## 十、调研依据与采用范围

调研用于信息组织，不照搬产品 UI，也不引入新前端构建链。

- [LangGraph streaming](https://docs.langchain.com/oss/python/langgraph/streaming)：提供节点更新、消息及自定义流等能力。SSM 应从真实节点和消息投影事实，不能把所有调试事件直接当用户内容。
- [LangGraph Agent Chat UI](https://docs.langchain.com/oss/python/langgraph/ui)：直接识别并展示工具调用和 interrupt。借鉴其与真实执行对象绑定的方式，继续使用本项目 React 无构建架构。
- [ChatGPT deep research](https://help.openai.com/en/articles/10500283-deep-research-in-chatgpt)：进展、可干预任务和有来源的最终结果分别承担职责。SSM 借鉴“进展有事实、结果有来源”，不为简单聊天强加研究计划面板。
- [VS Code 1.109 的聊天与无障碍改进](https://code.visualstudio.com/updates/v1_109)：强调流式内容和可访问的工具调用信息。SSM 保留真实运行状态与键盘可访问详情；本文的成功折叠、异常展开是本项目设计选择。

## 十一、本次文档交付记录

- 已审查本计划列出的相关运行、适配、事件、存储、前端和回归入口，并核对关键调用引用。
- 已将“主 Graph 已有人格，不应另建人格包装流程”的要求纳入 P3 和删除清单。
- 本次新增计划文档，并在 `.gitignore` 增加此文档的单文件例外，便于纳入版本管理；此前工作区已有改动不视为本计划完成，也未覆盖现有运行资产。
- 文档验证：41 个命令中的路径均存在，代码围栏成对，无行尾空格，`git diff --check` 通过；未运行实施回归、重启服务或触发硬件。
- 实施进度以 P0–P6 的勾选、对应代码差异与新验证记录为准。
