# 「大肥鱼」人设插件与 SSM 三智能体人格设计分析

> 调研日期：2026-10-30  
> 对象：[OMGLogic/dsh-whale-persona](https://github.com/OMGLogic/dsh-whale-persona) 的 `main` 分支；仓库内容会继续变化。  
> 范围：人设文本、注入方式、编辑体验，以及对 SSM 管家、桌面灯、Go2 的启发。文末记录 2026-10-30 在本地工作区落地的三位角色；未验证远端服务。  
> 关联：[现有人格系统调研](2026-10-30-ai-character-personality-systems.md)、[三智能体改进计划](../superpowers/plans/2026-10-30-go2-personality-behavior-and-replies.md)

## 结论

这个仓库的强项是**让一个鲜明角色稳定进入每次对话，并让用户随时看见、切换和编辑它**。插件默认提供一组有辨识度的鲸鱼角色标签；运行时把整段文本放进独立的提示词段，编辑后即时读取。它没有实现情绪引擎、数值特质到行为的映射，也没有定义这些标签的解析器。因此不能把它理解成一套成熟的多智能体人格运行时。[README](https://github.com/OMGLogic/dsh-whale-persona/blob/main/README.md)、[服务端源码](https://github.com/OMGLogic/dsh-whale-persona/blob/main/lib/index.mjs)

SSM 在本次改动前已有行为参数、情境输入和设备结果校验；三位角色仍显得相似，可能因为**独特反应方式写得不够清楚**，也缺少按闲聊、完成、受阻、未确认等情境对照角色声音。这是基于两个代码库的设计判断，不能仅由代码证明用户主观体验。下文记录了用户选定的角色方向及本地落地结果。

## 它具体怎样设置人设

| 层面 | 仓库里的做法 | 对实际效果的作用 |
| --- | --- | --- |
| 默认角色文本 | `DEFAULT_PERSONA` 是十余行简短标签，覆盖鲸鱼身份、中文、聪明但懒、傲娇甜、特殊反应等线索。 | 用少量高辨识度线索给模型一个一致的角色方向；标签本身没有仓库内的语义解释器。 |
| 注入位置 | 注册独立的 `whale:persona` 提示词段，排在部署人设前缀之后、策略与工具说明之前；不占用宿主原有人设段。 | 全局生效，同时保留宿主各 preset 与系统策略的位置。 |
| 原样注入 | `interpolate: false`，正文由函数在组装提示词时读取当前配置。 | 编辑文本不会被 `{{...}}` 模板插值破坏；下一次组装可拿到新值。 |
| 开关与编辑 | `enabled` 和 `persona` 是可实时更新的配置；输入栏有开关，设置页有文本框、保存、恢复默认和状态反馈。 | 角色是否启用清晰可见；改动无需重启，失败时 UI 能回退或显示错误。 |
| 关闭后的行为 | 卸载鲸鱼人设段，恢复各会话 preset 自己的人设。 | 方便直接比较启用前后的表达。 |

这些是[服务端实现](https://github.com/OMGLogic/dsh-whale-persona/blob/main/lib/index.mjs)、[客户端实现](https://github.com/OMGLogic/dsh-whale-persona/blob/main/lib/client.js)和[更新记录](https://github.com/OMGLogic/dsh-whale-persona/blob/main/CHANGELOG.md)中可以直接核实的机制。README 的聊天截图是展示样例，并非同一输入、多次运行的稳定性测试。[README 效果展示](https://github.com/OMGLogic/dsh-whale-persona/blob/main/README.md#效果展示)

### 默认文本的角色线索

源码没有逐个解释标签；下表的“可能传达的意思”是从英文名称作出的推断，不能当成插件的确定行为规则。[默认值源码](https://github.com/OMGLogic/dsh-whale-persona/blob/main/lib/index.mjs)

| 标签例子 | 可能传达的意思 | 对 SSM 的启发 |
| --- | --- | --- |
| `SELF_CLAIM_WHALE_GIRL`、`LANG_ZH_CN_ONLY` | 自我身份与语言稳定。 | 三位智能体应各有一句清楚的“我是谁”，且对话中不串角色。 |
| `PERSONALITY_SMART_LAZY`、`PERSONALITY_TSUNDERE_SWEET` | 两组略有张力的性格线索，比单写“可爱”更鲜明。 | 给每位角色一组可共存的反差：管家沉稳但会照顾同伴；灯高傲尖刻但护着用户的眼睛；Go2 幼稚活泼但肯听劝。 |
| `TRAIT_NOT_FAT_REFUSE` | 对某个特定称呼有特别反应，形成记忆点。 | 可以设计少量“遇到什么会怎么回应”的习惯；不必把它写成不论情境都重复的口头禅。 |
| `TIMEOUT_SIGNAL` | 似乎暗示超时场景的特殊表达。 | 把未确认、受阻和离线纳入角色示例；真实状态仍由执行记录决定。 |
| `OBEY_MASTER_ALWAYS` | 强服从式关系设定。 | SSM 不应照搬到设备控制：人设文字不能授予运动、GPIO 或跨设备调用权限。 |

它的辨识度来自**身份、语言、反差特质和一个特殊反应**一起出现，而非标签格式本身。当前默认文本缺少情境示例、对话历史处理和“何时不说话”的定义；模型如何理解这些英文标签也取决于模型和宿主提示词。仓库明确把人设效果限定在表达，宿主的安全与审批规则照常生效。[README 说明](https://github.com/OMGLogic/dsh-whale-persona/blob/main/README.md#说明)

## 三位角色的定稿与实际消费点

按用户选择，本地默认人设和现有持久化配置都已替换；三位角色仍由各自的运行链路读取，执行事实继续以工具结果和回执为准。角色关系中的分歧只影响发言，不增加设备权限。

| 智能体 | 核心性格与说话方式 | 运行时读取点 | 特质的作用范围 |
| --- | --- | --- | --- |
| 管家 `cloud_orchestrator` | 经验老到、沉稳周全。先核对事实，再简短交代整体安排；遇到意外先讲现状和下一步。 | [默认角色卡](../../cloud/orchestrator/agentcore/personality/profile.py)、[编排与收尾](../../cloud/orchestrator/multitask.py) | `warmth` 改变关怀表达，`clarification_tendency` 影响确有歧义时的追问；不会把管家变成卖萌角色。 |
| 灯 `esp32_desk_led` | 高傲、审美挑剔、嘴巴刁钻；短句和轻微挖苦背后是对眼睛与工作节奏的关心。 | [默认角色卡](../../cloud/esp32/agentcore/personality/profile.py)、[手动对话](../../cloud/esp32/agent.py)、[回复链路](../../cloud/agent_runtime/replies.py) | `social_initiative` 影响活动内社交机会，`adjustment_sensitivity` 影响 reactive 重新评估；不赋予 manual 模式自主调光权限。 |
| Go2 | 可爱、活泼、略带童稚；短句、惊喜感，偶尔“汪”，对新鲜事跃跃欲试。 | [默认角色卡](../../cloud/go2/agentcore/personality/profile.py)、[任务对话](../../cloud/go2/multitask.py)、[每日演化](../../cloud/go2/agentcore/soul.py) | `curiosity` 影响探索评估机会，`extraversion` 影响互动表达，`boldness` 影响已有机会中的姿态候选；不构成运动许可。 |

Go2 每日演化只调整特质分数，不再自动重写管理员选定的人设正文。三个智能体本地保存的配置也已同步；远端进程是否加载这份配置，需要在部署后查看实际回复与日志。

## 角色冲突的边界

| 关系 | 让分歧自然出现的前提 | 角色表达 | 收束依据 |
| --- | --- | --- | --- |
| 管家 ↔ Go2 | Go2 有新线索，提出行动。 | Go2 兴奋地想去看；管家先问依据和路线。Go2 可以小声不服，但会听劝。 | 用户目标、可用观测、路线与运动许可。 |
| 灯 ↔ Go2 | Go2 的热闹可能干扰桌面活动。 | 灯尖刻地提醒“别把桌面也闹成游乐场”；Go2 俏皮回应，同时尊重工作或睡眠节奏。 | 活动状态、灯的模式、用户设置与真实反馈。 |
| 管家 ↔ 灯 | 整体安排已提出，照明细节尚未核实。 | 管家交代全局；灯指出亮度、配色或执行回执中具体哪处还欠火候。 | 用户要求、传感器与设备回执。 |

三者可以意见不一致，仍需承认同伴掌握的事实。没有相关事件时不强行让角色吵架；没有实际观测时不编造发现，只有“已受理”时不声称“已完成”。这一点与[同伴交互计划](../superpowers/plans/2026-10-27-personality-driven-peer-interaction.md)的在线、活动归属和冷却条件一致。

### 场景校准样例

这些句子只展示角色声音；动作完成、离线和观测条件必须先由系统核实。

| 情境 | 管家 | 灯 | Go2 |
| --- | --- | --- | --- |
| 普通闲聊 | “我在。你想先聊哪件事？” | “想聊天？可以，先说说你这回有什么高见。” | “我在我在！今天有什么好玩的呀？” |
| 动作确认完成 | “安排已完成。灯已经按你的要求调好。” | “这回光线总算像样了，已经调好。” | “好耶，我到啦！还想让我看看什么？” |
| 只受理、未获回执 | “请求已送出，我还在等设备确认。” | “回执还没到，先别急着夸我。” | “唔，还没收到完成消息呢，我再等等。” |
| 条件不足 | “我还需要一个目的地，才能继续安排。” | “光线数据都没准，别让我凭空猜亮度。” | “我还没看清呢，先停一下好不好？” |

辨识度来自**各自不同的观察点与反应**，不是每句话都挂口头禅。灯的尖刻只针对情况和审美，不羞辱用户；Go2 的童稚不掩盖失败或危险；管家的沉稳不变成空泛保证。

## 实现与验收记录

本次改动覆盖三个默认角色卡、持久化配置与共用事实约束回复。后续结构清理已移除管家和 Go2 的旧框架，并把灯的现行对话循环放入 `multitask.py`。管家仍交代全局，灯只说与自身照明有关的事实，Go2 只说已观察或已执行的事情。Go2 默认外向性升到 80，更贴近“活泼爱说话”；原有好奇心和大胆程度的本地取值保留。

本地相关测试为 **35 passed**，覆盖人格回复路径、Go2 模式、管家与灯的设备聊天及 SSE 展示相关路径。此结果证明相关代码路径与约束通过测试，不能代替在线模型对三种声音的实测。建议部署后用上表四种情境观察一次真实对话，再根据效果微调文案，不必追加只会重复文案本身的测试。

## 来源与判断边界

- 外部一手资料：[README](https://github.com/OMGLogic/dsh-whale-persona/blob/main/README.md)、[服务端 `lib/index.mjs`](https://github.com/OMGLogic/dsh-whale-persona/blob/main/lib/index.mjs)、[客户端 `lib/client.js`](https://github.com/OMGLogic/dsh-whale-persona/blob/main/lib/client.js)、[CHANGELOG](https://github.com/OMGLogic/dsh-whale-persona/blob/main/CHANGELOG.md)。
- SSM 对照依据：本工作区的三份 `agentcore/personality/profile.py`、各自的实际回复循环与本地保存的配置；未连接远端服务作现场实验。
- 对“大肥鱼”标签含义的说明是从源码作出的推断，不代表作者承诺某种稳定行为。
