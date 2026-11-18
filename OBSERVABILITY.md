# 当前对话事实与排查（2026-11-18）

推物运动已移除逐轮货物关联、私有通路地图重建和接触许可链，接近、站位和前进直接读取普通 Navigator 的共享地图。货物观察也复用已完成的共享地图，普通地图更新仍在传感器后台完成。速度输入的 0.3 秒看门狗保持原值；排查连续移动时核对实际 Move 间隔与 StopMove 原因，不再查找已移除的 cargo_contact 回执。坐标导航首次发速度前确认当前连接的 ClassicWalk，等待结束后重新检查定位和控制归属，连续速度更新不等待步态 RPC。

每次连接及重连后的首次运动准备记录 `[Go2/Gait] ClassicWalk api=2049 enabled=true code=... request=...`。必须先有 `code=0`，随后才允许摇杆与任务 Move；拒绝、缺失成功码或超时阻止本次移动，保留连接及停止能力。连接握手阶段不切换步态，常规运动期间不重复设置步态。配置回执和实际腿部遥测分别判读。

管理员可通过 `POST /api/go2/diagnostics/classic-walk-test` 做一次真实运动诊断：停稳后发送 `ClassicWalk(true)`（2049），仅在请求回执成功后沿共享 Navigator 以前进 0.3 米、0.3 m/s 为目标移动，并核对停止遥测。入口拒绝已有运动租约；沿用可信用户代次与取消链，不调用模型。`[Go2/Gait]` 保存请求回执及测试结果，步态请求被接受不代表实测步态已确认；成功选择的经典行走保持开启，不自动切换其他步态。

用户目标以服务端 `goal_revision` 区分，和防止旧活动重开的 `user_revision` 分开追踪。排查新请求被旧任务挡住时，先核对签名意图、内部 HTTP 和原 Graph 的代次；旧目标的模型等待及排队请求应取消，迟到回复不得进入新目标。取消等待不证明设备停止，已派发动作的真实／未知结果仍在原运行与账本查询。Go2 控制交接按原租约 generation 等待清理和新鲜速度停稳证据；`stop_unconfirmed` 保留运动占用，不能靠租约超时推断停稳。

活动的 `message` 表示公开台词，其中游戏阶段台词不一定经过同伴消息总线；真实同伴通信另有 `message_id/conversation_id/reply_to` 与送达回执。中间决策可以不发言，`action` 只记录工具及参数，`action_result` 保留真实结果，可选 `decision_basis` 只进过程详情。同一轮选择说明和台词分开，不能用台词证明物理动作完成。

浏览器展示的是原运行的有界公开事实，不是日志或模型提示词。共享 `progress_facts.py` 负责调用、参数、结果与关联字段的公开投影，沿用 `agent_progress`、`agent_reply`、`run_stage` 与现有 MQTT/SSE 通道。`stage` 仍可用于忙碌、追问和终结控制；没有实际内容时不新增聊天记录。原模型的公开决策说明可缺省，不为进展另调解释模型。

执行失败的详情直接取真实工具回执，不依赖人格回复复述。模型服务错误保留 HTTP 状态码、错误码、异常类型和服务商原因；Go2 搜索异常沿用这些详情。公开错误正文上限为 8192 字符，关联字段仍保留在事件中用于合并与追踪；浏览器只移除正文中的 request_id、task_id 等关联标识，不能因此丢弃整段原因，也不隐藏英文错误或错误码。历史缺少原因的回执只能展示已有信息。

游戏过程只展示本局实际产生的公开约定（例如猜拳的颜色／动作映射及确认）、观察、选择和真实结果。静态游戏介绍、完整规则和执行流程不作为查询结果写入时间线，公共投影不输出 `game_rules.description/rules`；PWA 也忽略历史事件中的这些字段，只有静态规则的旧事件不生成过程条目。

游戏执行器确认开局后记录 `game_started`，关联原邀请及任务的最终同伴回执投影为 `game_ended`。PWA 对应显示“本局游戏已开始”和“本局游戏已结束”；异常结束另列真实原因，不追加“已取消”或后续活动的解释性话术。断点不结束外层空间活动，过程不能跨断点合并到后续发言。历史配送终结 `response` 和游戏 `settlement` 使用其明确的游戏／邀请关联恢复结束断点，不从聊天措辞或单步动作失败猜测开始、结束。

灯色、亮度是持续设置。在线灯的实际 MQTT 状态回执以 `validity=until_changed` 投影，保持原 `as_of`，不会因超过通用观察的 60 秒而失效。离线会撤销本连接的确认，重连后等待新的实际状态；retained 回放不刷新观测时间。空间快照、同伴视图、决策证据和活动历史共用这一判定。传感器及 Go2 连续遥测仍按原观测／定位时效处理。配送信号不可确认与实际颜色变化分别报告，不合并成同一错误码。

公开能力名称由公共事实投影统一生成：公开 Go2 技能沿用 Card 名称，内部工具使用同一工具声明模块的公开名称解析，不为展示新增可调用技能。SSE 和历史查询都补齐名称，旧文件不迁移；前端聊天、过程详情及设备能力说明共用名称转换，未知工具显示“设备操作”，不会回退到原工具 ID。

导航暂停后身体确认暂缺时，保持零速度并沿用现有遥测恢复窗口等待；收到真实身体反馈后重规划同一任务，不重发整个物理动作、不等待 Sport RPC 回包，也不把停止指令当成站姿证明。恢复期间仍检查控制租约、任务期限和定位／地图会话；身体动作受阻保留原原因，不统一报告为连接异常。

移动顿挫排查区分目标速度、平滑后的实际 Move 和 StopMove。Navigator 的 `navigation.progress` 保留实际 `last_command`；运动暂停记录 `phase=motion_paused`、原速度与原因（例如 `odometry_not_updated`、`plan_pose_changed`、`body_clearance_replan`）。指令看门狗到期另有 `[Go2/Motion] velocity input watchdog expired` 日志。重复定位包不会续发 Move，也不会每包触发一次停止／重启；0.3 秒指令看门狗保持有效。周期重规划在后台执行，慢规划期间仍须用最新地图逐步验证当前运动。原始 Sport 日志可核对 `1008=Move` 和 `1003=StopMove`；停止指令数量不能直接当作实际停稳次数。

`walk_turn_transition` 表示平移与原地转向的必要交接停车；后续动作仍等待停稳证据。导航控制目标间隔 50 毫秒，相同运动状态不随每个 Move 重复广播。地图新包与已完成地图分别计时；建图替换期间的旧快照仍须满足原有 2 秒观测有效期，不再仅因落后最新包 0.3 秒而判为不可用。

目的地导航的 `motion_mode=turn_then_forward`，相对移动为 `forward/backward/left/right/turn_left/turn_right`；CLI 同时展示实际 `last_command`。任务每次只允许 x/y/z 中一个非零，平滑后仍混轴会以 `mixed_motion_command` 停止，不能静默裁掉分量后宣称动作成功。明确的相对位移按原请求轴上的真实里程计投影核对距离；相对转向只核对转角及停稳，不为回到转身前的 XY 坐标补发平移动作。摇杆组合输入保持用户的原始意图，仍走既有授权和停止链。

核对转向时同时看货物方位、站位目标和路径前瞻点；`navigation.progress` 记录 `pose_heading_rad/lookahead_odom_xy/route_heading_error_rad`，不能用“货物在右侧”推断绕行路径必须右转。行走步态排查使用运动期间每秒一条的 `[Go2/MotionTelemetry]`：保留实际 mode、gait_type、body_height、foot_raise_height、速度、IMU、四足受力与电机诊断及 LOW_STATE 年龄。未提供的字段为 null，不用零值冒充实测受力；指令速度、里程计位移和真实腿部遥测分别判读，不从低速或停转次数推断电机故障。

排查时先核对 `trace_id/run_id/parent_run_id/step_id/task_id`，再看源事件身份、模型轮次和顺序。`agent_id` 是执行者，`target_unit_id` 是目标；消息送达、活动成员加入和物理动作回执分别判定。工具外层成功可能包含成员失败，不能只看外层 `ok`。历史缺失证据不会重新生成说明；迟到回执只补原任务，不重新派发动作。

配送雷达关联通过 `vision.lidar` 的 `phase=cargo_foreground_association` 记录目标框、匹配结果和点簇筛选诊断；`cargo_geometry/cargo_delivery_geometry` 的 `vision.model` 解析事件保留实际货物／目的地字段。`cargo_returns_unavailable` 只能证明本次无法可靠分离货物，具体原因须核对 `association_diagnostics`，不能仅凭错误码断言过薄、太远或硬件故障。消息发送失败与活动加入结果分别展示。

`make logs` 的最新会话和 `make task-trace` 默认追踪识别浏览器页作用域 `chat_` 和登录作用域 `ses_`，排除命名测试样本及 system 日志。`make trace-task ID=<session_id>`、`ID=<trace_id>` 仍支持明确查询，附加 `FOLLOW=1` 可固定目标并追踪新事件，避免新页切换排查目标。CLI 回归使用临时日志，不连接设备。

以下是改造前的历史基线，不表示当前运行入口或本次验收结果。

# 任务日志现状基线（2026-10-26）

实施前的一条灯到 Go2 请求可从灯运行 ID `32e9468268f044ab9b1bbefe4035106c` 和同伴 `request_id=dance-go2-001` 找到部分信息。`logs/api.log` 的 `cloud.agent_runtime.loop` 行只写工具结束与结果；同文件里的 `cloud.peers.gateway` 行写 `needs_clarification`。同一文件有多条 Go2 任务 HTTP 202 轮询，最后返回 200。旧日志没有统一的 `trace_id`、每轮模型输入输出、父子运行 ID 或可核对的最终发送文字，不能从旧记录反推这些字段。

| 来源 | 改造前内容和路由 | 当时大小 |
| --- | --- | ---: |
| `logs/api.log` | `make api` 的 `tee`、API 访问、共用循环与同伴网关 | 443 KiB |
| `logs/esp32.log` | API `FileHandler` 单独写灯 logger | 867 KiB |
| `logs/go2.log` | API `FileHandler` 单独写 Go2 logger，含高频状态和旧图日志 | 7.0 MiB |
| `logs/orchestrator.log` | 后台 stdout；独立进程用 `basicConfig` | 73 KiB |
| `logs/mosquitto.log` | Broker 原始日志 | 186 MiB |

现有 tmux 辅助命令只有 `go2-logs-window`，按窗口名增补 `go2-events` 和 `go2-state`；`make trace` 输出全量 MQTT，`make logs` 分文件 tail/grep。tmux socket 在实施环境内无访问权限，因此无法记录实际会话窗口列表，也不改动任何现有会话。

入口映射：`/api/intents` 在 `cloud/api/intents.py` 接收主聊天并发布 intent；灯设备页调用 `cloud/esp32/router.py` 的 `/{unit_id}/chat`，由 `cloud/esp32/multitask.py` 处理；Go2 设备页调用 `cloud/go2/router.py` 的 `/chat`，由 `cloud/go2/multitask.py` 处理。编排器 MQTT intent 与 peer 在 `cloud/orchestrator/main.py`，逐步循环在 `cloud/orchestrator/multitask.py`。历史日志中的 `[Go2/Prompt]`、`[Go2/Chat/Reply]` 和旧图 Responder 不代表当前回复路径；对应旧入口已删除。

可复用的现有标识是 `run_id`、`step_id`、peer `request_id` 和设备 `task_id`；旧记录没有可信根 `trace_id`、`parent_run_id` 或完整轮次数据。迁移前的 SQLite 文件为灯语义任务 `semantic_tasks.sqlite3`（若存在）、灯/Go2 `data/multitask.sqlite` 和编排器配置路径所指文件。它们承担幂等和恢复语义，须先迁移核对再切换运行入口，旧文件在真实验收后才可清理。

本机切换时未发现运行中的 API、编排器或 Broker；已备份灯与 Go2 的旧 SQLite 文件及附属 WAL/SHM 到各自 `data/legacy_sqlite_backup_2026-10-26/`，再迁移灯 3 个 run/4 个步骤、Go2 15 个 run/2 个步骤。迁移后逐条用 `RunStore.get` 查询核对；原 SQLite 文件仍保留。本机没有灯语义任务库或编排器运行库，部署环境若有这些文件需按 README 的步骤另行迁移。真实灯/Go2 成功与失败任务、tmux socket 及服务重启后的时间线尚未在本环境验证，旧日志在这些验收完成前保留。
