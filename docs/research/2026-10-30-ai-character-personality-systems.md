# 成熟 AI 角色与人格设计调研

> 调研日期：2026-10-30  
> 用途：为 SSM 的编排器、ESP32 灯、Go2 三个智能体提供设计参考；实现安排见[人格与行为改进计划](../superpowers/plans/2026-10-30-go2-personality-behavior-and-replies.md)  
> 方法：阅读项目官方文档；以下是具有代表性的案例，不是全网穷尽排名，也不宣称其角色行为在所有模型下稳定复现

## 概念框架

- **人格/角色身份**：智能体是谁、与用户是什么关系、承担什么职责，以及稳定的表达风格。
- **性格特质**：相对稳定的反应倾向，例如好奇、外向、谨慎。分数应对应可观察的偏好，不等于无条件执行命令。
- **当前状态**：任务、场景、交互对象、记忆和设备状态。它解释同一种性格为什么在不同情境下有不同表现。
- **行为与回复**：当前实际采取的动作、保持安静或说出的内容。实体设备还要经过权限和设备反馈验证。

关系可概括为：角色身份和特质影响候选与表达，当前情境决定何时适用，执行边界决定能否落地。以下项目对这几个环节采用了不同的拆法。

## 1. SillyTavern：角色卡与场景示例

[官方角色设计](https://docs.sillytavern.app/usage/core-concepts/characterdesign/)把角色描述、性格、场景、开场白和示例消息分开。角色描述、性格与场景属于持续进入上下文的设定；示例消息展示角色怎样说话，但可能随上下文挤满而退出。角色卡还可关联[World Info／Lorebook](https://docs.sillytavern.app/usage/core-concepts/worldinfo/)，根据关键词或其他条件注入相关背景；官方也提醒注入背景不保证模型一定照着输出。[群聊说明](https://docs.sillytavern.app/usage/core-concepts/groupchats/)区分当前说话角色的卡与合并所有角色卡，并提示合并可能造成角色混淆。

**设计启发：**长久稳定的身份与临时场景应分开；用几条具体对话示例比堆砌“活泼、可爱”等形容词更容易看出角色声音。对 SSM，借鉴简短人设和场景验收即可；角色卡并不提供实体运动许可、定位或动作结果判断。

## 2. Character.AI：角色属性之外还有用户与会话

[官方 Character Guide](https://book.character.ai/)将角色属性、对话中的训练反馈、用户 Persona 和当前会话上下文都列为影响回复的因素，并提供快速创建与高级编辑两种路径。

**设计启发：**人格设定不会独自决定每次回复；同一个 Go2 面对正在工作的人和主动与它互动的人，表现可以不同。SSM 已有会话、活动与感知数据，优先利用这些事实，不必再造一套大型角色训练系统。

## 3. Convai：可调特质、情境状态与可观察上下文

[官方特质文档](https://docs.convai.com/api-docs/convai-playground/character-customization/personality-traits)提供预设和开放性、细致程度、外向性、亲和性、敏感性等数值维度，供角色表现调节。[角色配置总览](https://docs.convai.com/api-docs/convai-playground/character-customization)还将角色描述、语言、知识、临时心境、记忆、叙事目标、外部 API 分成不同配置区。[Mindview](https://docs.convai.com/api-docs/convai-playground/character-customization/mindview)展示实际送入模型的角色和上下文内容，用于排查设定没有生效或相互冲突的情况。

**设计启发：**数值特质适合成为 UI 参数，但应有清楚的行为含义和生效范围；同时记录运行时实际使用了什么配置。SSM 无需复制 Convai 的全部维度或情绪轮，只需要已有或必要的少量参数。

## 4. Inworld：角色资料进入目标和动作流程

[官方迁移说明](https://docs.inworld.ai/character-studio-deprecation)指出旧 Character Studio 已退役，现以 Agent Runtime 的模板和应用逻辑组合角色、知识、安全、长期记忆、情绪及关系等能力；触发器、变量和集成由 Runtime 图与应用逻辑承载。其[角色交互示例](https://dev.docs.inworld.ai/Unity/runtime/demos/primitives/character)在角色创建中填写名称、角色、描述、动机，再把角色数据加入对话提示词。其[Unreal 示例](https://dev.docs.inworld.ai/unreal-engine/runtime/templates/innequin)展示用意图激活问候等目标。

**设计启发：**实体角色需要让“我是什么样的角色”影响“我现在有什么目标和候选动作”，同时让实际动作仍由应用运行链负责。SSM 已有自己的 Drive、工具、导航和设备卡，可在这些节点接入人格，不需要迁移到 Inworld Runtime。

## 5. Letta：人格与用户记忆分开

[官方记忆块文档](https://docs.letta.com/v1-sdk/memory/memory-blocks)将 `persona` 与 `human` 建议为不同的持久上下文块；块可由智能体管理，也可设为只读。前者描述智能体自己，后者记录用户相关事实与偏好。

**设计启发：**“Go2 喜欢探索”属于自身人设，“用户不喜欢晚上被打扰”属于用户偏好；后者不应被自动改写成前者。SSM 可继续使用已有记忆模块，但持久人格配置应独立保存。

## 对 SSM 的结论

| 可借鉴的机制 | 落到 SSM 的最小做法 | 不宜照搬的部分 |
| --- | --- | --- |
| 酒馆的角色身份与场景示例 | 每个智能体一份短人设，验收时准备成功、失败、安静、被打断等示例 | 将大量世界书或角色指令直接作为运动控制规则 |
| Convai 的特质参数与上下文检查 | 少量有消费点的特质，设置页显示含义；日志记录决策时所用配置 | 为了“像人格系统”而增加没有行为映射的滑杆 |
| Inworld 的角色目标与动作衔接 | 在现有 Planner/ReactiveMind/Drive 的决策点读特质；实际执行仍由设备链校验 | 用人设文字直接改变速度、避障、工具权限 |
| Letta 的人格与记忆分离 | 自身人格独立于用户偏好和历史事实 | 把近期对话自动当成稳定人格修改 |

这些是依据官方文档对 SSM 作出的工程判断。可验收的标准是：更改一个特质后，在相同情境中能观察到对应的候选行为或表达变化；更换情境时角色仍可合理保持安静；真实动作失败或未确认时，角色化回复仍然准确。
