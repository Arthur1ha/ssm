# Go2 阶段 A＋B 契约、实测证据与交付记录

- 日期：2026-11-06
- 对应计划：[`2026-11-06-go2-hierarchical-state-and-actions.md`](2026-11-06-go2-hierarchical-state-and-actions.md)
- 本轮范围：A 基线与契约冻结、B 统一动作目录与身体层扩充。
- 验证边界：无硬件自动测试；本轮未连接、未重启服务、未执行真实运动。
- 保持现有 LangChain/LangGraph 大脑、执行上下文、控制租约、任务结果协议。
- C 的统一运行快照与连接同步、D 的生活层重组、E 的完整分层对外协议、F 的前端改造均不在本轮实施范围。

## 1. 初始工作区与保护边界

开始时 `git status --short`：

```text
 M .gitignore
 M scripts/probe_go2_actions.py
?? docs/superpowers/plans/2026-11-06-go2-hierarchical-state-and-actions.md
```

原有 `.gitignore` 增加计划文件白名单，探测脚本有 409 行规模差异（348 行增加、62 行删除）；这两项不回退。脚本和本地 JSONL 仅只读检查，不执行交互探测。原计划文档保留；本记录另行补充实施证据。

开始时探测脚本 SHA-256：`6545ef0880436b23ca5b83af354166899643b99a82156cf8b7e0d00068ee89bc`。开始时 `.gitignore` SHA-256：`d0f8a767e191ebf6ff563cb0cad4efba8227ed1518dd44677f337d929ed903b8`；本轮只在其已有增量基础上追加本记录的精确白名单。

## 2. 冻结状态与转移契约

以下是 A 阶段冻结的目标接口契约。声明契约不表示本轮已经实现 C–F 的分层运行时。

| 维度 | 稳定 ID | 含义与缺省语义 |
|---|---|---|
| 连接 | `offline / connecting / online` | 控制连接事实；连接成功不是姿态证据 |
| 活动 | `active / rest` | 是否允许普通活动；离线为空，与空间 `activity_id` 无关 |
| 生活 | `idle / play / explore` | 任务目的；休息或离线为空，与身体状态并行 |
| 身体稳定姿态 | `standing / lying / sitting` | 已确认姿态；`sitting` 不能由身高阈值猜测 |
| 身体运动 | `moving` | 实际运动，不能仅由进入遥控推断 |
| 身体有限动作 | `greeting / stretching / content / dancing1 / dancing2 / finger_heart / pouncing / jumping` | 当前执行事实，不能将请求目标当作完成姿态 |
| 身体辅助状态 | `unknown` | 缺少可靠姿态/完成证据；不计入 12 个身体业务状态 |
| 自主配置 | `manual / free_explore` | 继续沿用原 API 值；允许自主不表示此刻正在探索 |
| 控制模式 | `normal / remote` | 遥控入口与实际移动分开；具体运行时在 C 落地 |

普通身体转移：

- `lying --StandUp--> standing`，`standing --StandDown--> lying`。
- `standing --Sit--> sitting`，`sitting --RiseSit--> standing`。
- 只有站立能开始 Move、Hello、Stretch、Content、Dance1、Dance2、FingerHeart、FrontPounce、FrontJump。
- 有限动作只有可靠完成证据才能确认预期结束姿态；普通 StopMove 中断按动作目录逐项声明，不能默认新动作可打断。
- 趴下/坐下直接跳舞、坐下直接移动、有限动作之间直接切换均阻止；底层不隐式补起立等动作。
- 急停保持原授权入口可达，停止请求不代表站立证据，不能将坐姿、趴姿、跳跃无条件改写为站立。

休息、唤醒及探索恢复默认值（本轮只冻结，不增加生活控制入口）：

| 事件 | 冻结处理 |
|---|---|
| 请求休息 | 仅在线、可活动、生活待命、身体已确认稳定、无未完成动作/导航、权限和控制权允许时接纳；保持连接和当前姿态，不附加 StandDown |
| 忙碌时休息 | 直接阻止并解释；不排队、不隐式强停、不稍后执行 |
| 唤醒 | 回到 active＋idle；不附加 StandUp，不恢复旧动作或旧探索 |
| 普通互动结束 | 回待命；之后是否探索由既有自主策略决定，不由身体状态机自行恢复 |
| 明确停止探索 / 急停 / 休息 / 关闭自主模式 | 清除继续探索意图；互动结束不得自行重启 |
| 探索中用户介入 | 交接所属导航/速度流，停步未确认不得开始下一身体动作；任务归属按原 session/task ID 保留 |
| 断连 / 重连 / 服务重启 | 旧未完成动作不重放；新连接代次拒绝旧完成回调，身体从真实反馈重建 |

兼容与结果：

- `fsm_state` 保留为兼容字段；C 落地后从统一快照单向投影：离线/连接中用旧占位，在线投影身体状态。禁止双向同步。
- HTTP `/api/go2/connection`、现有 SSE 与 MQTT state 后续来自同一快照；Card 仅声明静态能力与图，不复制动态状态。
- 未来快照字段固定为 `state_schema_version / epoch / revision` 版本语义：同代次 revision 单调递增，新 epoch 不能被旧 revision 压制。
- 继续使用现有 `ok / accepted / executing / blocked / rejected / timeout / cancelled / error` 等结果值；`ok` 表示已确认完成，`accepted` 表示已接受但完成未确认，`timeout` 表示对应等待超时。`confirmation` 必须说明证据范围。
- RPC code=0 仅表示请求接受；只有经协议与实测证实为完成回执的动作才允许提升为完成。本轮现有记录不足以支持这样的提升。
- 不使用固定延时回站立；不自动重发超时动作，不让未确认结果放行依赖动作。

## 3. 已有真实测试证据

只读取以下两份本地记录，共 45 行；原始文件属于被忽略的运行数据，不将原始数据或认证配置加入版本控制。

| 文件 | 行数 | UTC 时间范围 | 特征 |
|---|---:|---|---|
| `cloud/go2/data/action_probes/my_air.jsonl` | 13 | 01:42:57–02:01:01 | 姿态、恢复及参数探测 |
| `cloud/go2/data/action_probes/my_air_sport_actions.jsonl` | 32 | 02:10:27–02:19:57 | 表演、跳跃及其他探测 |

下表 `A` 指 `my_air.jsonl`，`B` 指 `my_air_sport_actions.jsonl`；行号按 2026-11-06 读取时的内容固定。全部已有记录使用 **SPORT** 目录、`RTC_TOPIC["SPORT_MOD"]`，没有 MCF 实测记录；不得根据命令名静默改用 MCF 或切换运动模式。

| 命令 | SPORT API ID | 参数 | 原始证据 | 已知遥测 | 能证明 / 不能证明 |
|---|---:|---|---|---|---|
| Sit | 1009 | 无 | A:8，01:58:45 UTC，code=0、visual=y；A:6 另有较早 TimeoutError | 160 样本；progress 最大 0；mode 最后 0；身高 .271–.342 m | 用户观察坐下；没有机器可判定的坐姿完成信号 |
| RiseSit | 1010 | 无 | A:9，01:58:58 UTC，code=0、visual=y | 160 样本；progress 最大 0；mode 最后 0；身高 .258–.344 m | 用户观察起身；不能只因身高 ≥.25 m 认为完成，因为坐姿数据同样满足 |
| Content | 1020 | 无 | B:3，02:10:53 UTC，code=0、visual=y | 160 样本；progress 最大 0；mode 最后 0；身高 .310–.349 m | 动作可执行；缺开始/完成时刻、最终姿态，名称保持 Content |
| FingerHeart | 1036 | 无 | B:8，02:12:26 UTC，TimeoutError；B:10/14/30 为 skipped | 超时记录未含遥测和视觉字段 | 用户此前声明六动作可执行；文件没有正向执行或完成证据，不能将 timeout/skipped 算成功 |
| FrontPounce | 1032 | 无 | B:17，02:14:59 UTC，code=0、visual=y；备注原文 `n` | 160 样本；progress 最大 0；mode 最后 0；身高 .185–.382 m | 用户观察前扑；备注含义不推断，缺完成和实际中断证据 |
| FrontJump | 1031 | 无 | B:18，02:15:30 UTC，code=0、visual=y | 160 样本；progress 最大 0；mode 最后 0；身高 .121–.331 m | 用户观察前跳；不能用固定时长、RPC 或瞬时高度确认落地站稳 |

旧动作需同时承认的限制：Hello（B:1）、Stretch（B:2）、Dance1（B:4）都有 code=0＋visual=y，但 `max_progress=0 / last_mode=0`；Dance2（B:5）为 TimeoutError，之后跳过。不能继续把 Hello 的 RPC code=0 单独算作完成，也不能宣称这些固件记录支持完整 progress 开始/结束周期。

### 记录的证明范围

脚本将用户目视选择 `y` 记为 `observed`，并把 code=0 解释为“请求被接受，仍需目视确认”。8 秒默认观察窗是探测记录窗口，不是动作时长或完成判据。当前 JSONL 只保存摘要：样本数、最大 progress、最后 mode、身高范围；没有每帧时间、动作请求 ID、RPC 响应相对动作完成时刻、结束姿态、足部支撑或 StopMove 中断过程。

因此五个命令的目视成功不等于自动串联验收通过；FingerHeart 文件缺口也不推翻用户已有“可执行”的事实。共同待补的是可靠完成反馈、起止姿态和中断行为。

### 姿态反馈缺口

站立记录 BalanceStand（A:1）约 .322 m，Sit 最高 .342 m，RiseSit .258–.344 m；高度明显重叠。记录中 mode 均为 0，不能由该值区分坐下、站立或动作完成。现有 `body_height >= .25` 的站立确认策略不能直接套在 RiseSit 上；新增 Sit 也不能通过降低身高阈值伪造坐姿识别。

未取得可靠坐姿与恢复站立证据时，软件允许声明合法转移/发送请求，但只返回接受或超时/未确认，不伪造 `sitting/standing` 已确认事实，不开放依赖动作。独立外部姿态同步和完整连接初始化属于 C；不为解锁页面额外添加未经指定的恢复命令。

## 4. 状态读写与入口盘点

此表是 A02 的生产代码盘点；测试 stub 中直接设置 `fsm_state` 不列为生产所有权来源。

| 文件 / 位置 | 读写 / 依赖 | 本轮与后续处理 |
|---|---|---|
| `connection/fsm.py` | `_fsm_state` 初值、setter、available_actions/fsm_next；progress 和姿态回调 | B 从动作目录生成身体转移；完成判据不得凭 RPC 或时间 |
| `connection/webrtc.py` | 连接、失败、断连、发送、动作超时、Move、遥控归零、急停直接写 fsm_state | B 统一底层目录与动作确认；C 再处理所有连接/遥控语义与统一状态所有权 |
| `connection/sensors.py` | 回调驱动 progress/posture，投影 fsm_state 到 `_robot_state` 和订阅队列 | B 必要完成回调接线；C 统一代次和实时快照 |
| `tools/registry.py` | `_SPORT` 工具参数枚举和模型描述 | B 来自动作目录 |
| `tools/tools.py` | `_VALID_SPORT_CMDS`、大小写归一、租约、send_command 与结果投影 | B 来自动作目录，保留确认范围；移动定时只表示发送时长，不证明位移 |
| `tools/executor.py` | 参数 schema、连接/姿态/租约门禁、执行超时与结果规范 | 新动作沿用原边界；不增加第二运行时 |
| `router.py` `/commands /velocity /stop` | 命令入口、已安装库大表校验、共享门禁、急停/任务取消 | B 收敛为已声明动作集合；普通取消与紧急停止仍不同 |
| `router.py` connection / stream | HTTP 与 SSE 读取 fsm_state/available_actions | 本轮保持接口；C/E 后续统一快照 |
| `cloud/api/policy.py` | `GO2_IMMEDIATE_ACTIONS` 网关即时能力集合 | B 由目录生成身体动作集合；保留持久设置管理员边界 |
| `cloud/api/intents.py` | `/api/commands/go2`：SPORT 包装、规范动作名直派、STOP 和特殊路由 | B 验证新增命令贯通，不绕过可信服务身份 |
| `card.py` | MQTT state 摘要读取；FSM 图由 `_FSM_NEXT` 构造；动作标签/能力 schema | B 复用目录更新身体静态能力；不做 E 的分层 Card 或动态快照重组 |
| `cloud/agents/go2/{graph,prompts}.py` | 状态上下文只读 fsm_state | 保持现有大脑与框架；工具 schema 自动获得新能力 |
| `cloud/go2/perception/{visual_approach,visual_lidar_navigate}.py`、`tools/tools.py` | 只读 standing 门禁及遥测新鲜度 | 回归未知/坐姿不放行；不改导航算法 |
| `cloud/go2/navigation/`、`cloud/go2/mode/` | 本轮检索没有生产 fsm_state 直接赋值；通过连接/工具间接执行 | 仍需未来 C/D 关联任务、运动事实与生活归属 |
| `cloud/space/context.py`、`cloud/peers/view.py` | 允许字段列表投影 fsm_state/connected | 本轮保持兼容；E 扩展新状态摘要 |
| `cloud/peers/gateway.py` | 自主同伴 Hello、委托 Hello/Dance1/Dance2 的额外授权集合 | 保留原限制，不因本轮新增身体动作自动扩权 |
| `cloud/peers/games.py`、教导绑定与自主工作流 | 既有 StopMove/Hello 结果依赖 | 回归完成语义变化；新增命令不自动变为任意教导编排 |
| `app/src/pages/FsmDevicePage.jsx` | state_field 回退 fsm_state；accepted＋目标相等的旧局部投影 | 本轮不改前端；不得让未确认回复假称最终姿态，F 迁移确认展示 |
| `app/src/pages/fsmWidgets.jsx` | 连接控件读 fsm_state，局部 connecting/offline 展示 | C/F 后续分离遥控与运动 |
| `app/src/utils/agentMeta.js` | 通过 Card views 显示 fsm_state 标签 | 本轮保持平面图兼容 |
| `app/src/pages/{FsmGraph,StateVisual}.jsx` | 通过 Card 状态图渲染与姿态示意间接依赖 | 新动作完整示意、分层展示留给 F |

## 5. B 阶段实现与自动验证记录

### A06 初始无硬件基线

```bash
UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest \
  cloud/go2/tests/test_fsm.py cloud/go2/tests/test_connection.py \
  cloud/go2/tests/test_posture_confirmation.py cloud/go2/tests/test_tool_runtime.py \
  cloud/go2/tests/test_card.py cloud/go2/tests/test_access_control.py \
  cloud/go2/tests/test_router.py cloud/api/tests/test_gateway.py -q
```

初始结果：**93 passed，4 failed，7.09 秒**。失败记录：

| 基线失败 | 原因 / 处理范围 |
|---|---|
| `test_stand_command_waits_for_fresh_stable_height` 两个参数用例 | `_send_command` 姿态分支只返回 Future，调用者按 tuple 解包，触发 `RuntimeError: await wasn't used with future`；属于 B 涉及的发送/等待契约，集成时修复 |
| `test_stand_command_without_posture_feedback_remains_unconfirmed` | 同上；不能将这些原有失败算作新动作引入 |
| `test_session_cleanup_cancels_timed_chat_move_and_stops_robot` | `zero_velocity` 未被调用；会话清理/定时移动的原有问题，单列，不扩大为无关功能重组 |

### B 实现与最终回归

本轮软件接入完成，硬件确认能力保留明确限制：

| 变更 | 实现与验证范围 |
|---|---|
| `cloud/go2/actions.py` | 单一 `ActionSpec` 目录，12 个身体业务状态；旧 7 个 sport 工具动作＋指定 6 个新增命令；另保留 Move、EnterMoveControl、内部 BalanceStand，未开放底层库其他动作 |
| FSM / 发送 | 转移、来源、目标、映射、参数、等待上限与中断声明取自目录；Sit/RiseSit 不套身高确认；修复 StandUp/StandDown 返回 Future 未配对 response 的基线错误 |
| 未确认结果 | 新六动作及旧四表演的 RPC 成功只返回 `accepted`、`confirmation_scope=request`、`completion_supported=false`；身体为 `unknown`，`pending_action` 保存本次本地请求 ID、目标/执行态与阶段；不使用计时器或无关联 progress 确认完成 |
| 姿态确认 | 旧 StandUp/StandDown 保留请求响应后连续三帧高度判据；等待期间为 unknown，超时/取消保留未确认门禁；新 Sit/RiseSit 不使用该判据 |
| 停止与竞态 | 新六命令未确认中断能力时，普通 StopMove 被共享 preflight 阻止；原急停路径仍可发送停止请求，坐姿/趴姿保持原姿态，未知不写为站立；旧请求成功/失败/取消按动作代次与连接实例隔离，不污染重连后的状态 |
| 工具 / 网关 / HTTP | 工具 schema、说明、允许集合及直连校验复用目录；新动作沿用角色/租约/参数与状态门禁；结果元数据完整透传，不降格成“已执行” |
| Card | 身体拓扑、动作标签、能力 schema 与视频可见状态取自目录；保持平面结构，未增加生活分层或前端控件 |
| 可跟踪回归 | `actions_checks.py`、`action_gateway_checks.py`、`actions_graph_checks.py`；验证映射/拒绝/非法姿态/参数/租约/停止/晚响应，以及现有 LangGraph 在未确认后暂停依赖动作、相同 run 不重发 |

**B03 的交付边界**：坐下、坐姿起身的命令与保守结果契约已接入；目前没有生产反馈事件可将 Sit 后的 unknown 确认为 sitting，因此完整 Sit→RiseSit 自动链路仍待可靠硬件证据。测试中的 RiseSit 以显式坐姿事实作为前提，不代表已经实现真实坐姿识别。六个新动作不能标为“可靠串联通过”。

旧表演的行为也随事实契约收紧：Hello 不再将 RPC=0 当作完成；Stretch/Dance1/Dance2 不再根据无请求关联的 progress 回站立。动作后没有可靠姿态证据时，普通后续身体动作保持阻止，急停可用。旧四表演的 `interruptible=true` 是继承既有普通停止能力声明，仍需 H-AB04 补真实中断证据。

`pending_action.request_id` 是本地追踪 ID，不能声称原始遥测已经带回同一 ID。连接成功仍写 standing、EnterMoveControl 仍投影 moving 的遗留行为留到 C；**重连不是可靠的姿态恢复/动作完成证据**，不得据此判定本轮真机验收通过。

最终针对性命令：

```bash
UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest \
  cloud/go2/tests/test_card.py cloud/go2/action_gateway_checks.py \
  cloud/go2/actions_checks.py cloud/go2/actions_graph_checks.py \
  cloud/go2/tests/test_connection.py cloud/go2/tests/test_posture_confirmation.py \
  cloud/go2/tests/test_tool_runtime.py cloud/go2/tests/test_router.py \
  cloud/agents/go2/tests -q --tb=short
```

结果：**171 passed，11.63 秒**。包括六项通过真实 Graph→工具→模拟传输的串联/重入检查；没有调用远程模型或真机。

扩展回归另包括全部 Go2、Go2 Graph、API gateway/devices/teachings、Card、space、peer 测试及活动模式/视觉导航/同伴游戏的显式 `*_checks.py`。原始命令遇到 `cloud/peers/tests/test_personality_view.py` 收集错误（引用已删除的 `cloud.agents.go2.personality.legacy`），单列并排除后继续。扩展结果和未改代码对照见下表；未宣称全模块测试已经全绿。

| 检查 | 结果 / 说明 |
|---|---|
| 初始针对性基线 | 93 passed / 4 failed；其中 3 个姿态返回值错误本轮已修复；会话清理 `zero_velocity` 断言是原有顺序敏感失败 |
| 未改生产代码完整对照 | 在 `/tmp` 从 Git HEAD 生成独立副本，带入原有本地测试及相同配置环境；完整扩展基线为 **447 passed / 21 failed / 1 skipped，34.49 秒**。21 个失败包含本轮修复的 3 个姿态错误和下表的 18 个遗留失败；设备列表失败在相同运行顺序也能复现，单独运行通过 |
| 最终扩展回归 | **523 passed / 18 failed / 1 skipped，30.39 秒**；18 个失败与未改生产代码完整对照一致，本轮新增检查全部通过；修复基线 3 个姿态错误。另有上述旧 personality_view 收集错误，需显式排除后才可执行合集 |
| 静态与保护检查 | `git diff --check` 通过；探测脚本最终 SHA-256 与初始一致；未部署、未重启服务、未执行真机动作 |

扩展遗留失败分组（均在未改生产代码副本复现）：

| 本地测试文件 | 数量 | 遗留问题 |
|---|---:|---|
| `go2/tests/test_autonomy_inbox.py` | 3 | 游戏邀请确认语义和 messenger stub 与当前运行接线不一致 |
| `go2/tests/test_drive.py` | 1 | 导航调用断言缺当前 task_id / publish_reply 参数 |
| `go2/tests/test_episode_memory.py` | 1 | 旧日记清理断言与当前保留策略不一致 |
| `go2/tests/test_mode_runtime.py` | 3 | 导航参数、活动停止租约断言、已删除 light_change_bucket 导入 |
| `go2/tests/test_navigation_safety.py` | 5 | 失败原因与定位契约、任务归属停止及不完整 pose 断言 |
| `api/tests/test_devices.py` | 1 | 设备列表固定设备断言受前序全局 Card 状态影响；单独运行通过 |
| `cards/tests/test_registry.py` | 1 | state 精确相等断言未包含已有 `_received_at` 字段 |
| `peers/tests/test_inbound.py` | 1 | 引用已删除的 Go2 personality.legacy |
| `peers/tests/test_view.py` | 2 | stale / device_connected 字段断言与当前摘要契约不一致 |

新增检查曾暴露旧 Card 测试临时替换 router 模块后遗留包属性的测试隔离问题；已在新增 fixture 中按模块注册表取当前模块，最终针对性 171 项已覆盖同顺序的 Card→新增网关检查。

最终扩展命令（腾讯人脸 live 用例按原 opt-in 条件跳过）：

```bash
UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python -m pytest \
  cloud/go2/tests cloud/agents/go2/tests \
  cloud/api/tests/test_gateway.py cloud/api/tests/test_devices.py \
  cloud/api/tests/test_teachings.py cloud/cards/tests cloud/space/tests cloud/peers/tests \
  cloud/go2/actions_checks.py cloud/go2/action_gateway_checks.py cloud/go2/actions_graph_checks.py \
  cloud/go2/activity_mode_checks.py cloud/go2/perception/visual_approach_checks.py \
  cloud/go2/perception/visual_lidar_navigate_checks.py cloud/go2/perception/visual_target_checks.py \
  cloud/peers/games_checks.py --ignore=cloud/peers/tests/test_personality_view.py -q --tb=short
```

本地被忽略的旧 `cloud/go2/tests/test_fsm.py`、`test_card.py` 只同步动作全集、progress 不确认与急停不伪造站立的旧断言；新增可提交的 `*_checks.py` 包含对应新契约，不需要扩大 `.gitignore` 放开整批本地测试。

## 6. 真机补验清单

以下项目仅列为用户可观察、可接管窗口内的补验，不由本轮自动执行探测脚本。

| 项目 | 操作与应保留的证据 | 通过标准 |
|---|---|---|
| H-AB01 映射及回执 | 六命令逐项记录 SPORT API ID、参数、请求 ID、时间、RPC code、连接是否仍存活 | 与目录一致；拒绝/超时不被当成成功；FingerHeart 补正向原始证据 |
| H-AB02 Sit/RiseSit | 已确认站立发 Sit，真实坐姿确认后发 RiseSit；同步目视/视频和原始带时间遥测 | 找到能区分 sitting/standing 的可靠信号；只有确认后才开放依赖动作 |
| H-AB03 四个有限新动作 | Content/FingerHeart/FrontPounce/FrontJump 分别采集完整开始→执行→结束序列、结束姿态 | 证明完成判据确实属于当前动作；code=0/等待 N 秒本身均不通过 |
| H-AB04 普通中断 | 分别确认旧四表演 StopMove 的真实行为；新动作在获准和可接管条件下按目录补中断记录 | 不把软件 StopMove 接受等同于站稳；未知中断能力继续标限制 |
| H-AB05 旧动作回归 | StandUp/StandDown/Hello/Stretch/Dance1/Dance2/Move/StopMove | 原入口和租约仍有效；Hello 不因 RPC=0 虚构完成；Dance2 补超时后实际行为 |
| H-AB06 超时/断连 | 动作等待期间超时、断连，之后重连，观察日志与设备 | 无自动重发，无旧回调确认新动作；不确认身体姿态时不能继续运动链 |
| H-AB07 三入口一致性 | 受信任命令网关、Go2 聊天、管家请求同一新增动作 | 参数、租约、非法来源姿态和完成语义一致；无需前端新增按钮即可检查后端入口 |
| H-AB08 初始姿态缺口 | 连接时为坐下/趴下/站立，保留原始运动模式与姿态信号 | 为 C 定义可靠初始化；A＋B 不宣称已经完成统一连接同步 |

每项保存关联 request/task/run ID、真实起止时刻、原始反馈、用户观察、软件返回值和限制。H01–H12 原计划中涉及生活层/完整前端的项目继续留到 C–G，本次不做验收承诺。
