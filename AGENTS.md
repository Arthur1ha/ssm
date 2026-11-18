# SSM — Codex 工作指引

本文件保留修改代码必须遵守的约束；安装、启动、迁移见 [README.md](README.md)。启动以 [Makefile](Makefile) 为准，依赖以 `pyproject.toml` / `uv.lock` 为准，配置以 `cloud/.env.example` / `edge/config.py` 为准，消息与能力契约见 `protocol/`。行为变更同步维护相关契约，过程材料不代表当前实现或验收结果。

## 目录与架构边界

| 位置 | 职责 |
|---|---|
| `edge/` | MicroPython 固件：BSM 驱动、ISM 状态转换、TriggerMap 接线 |
| `cloud/agents/{steward,lamp,go2}/` | 三角色 Graph、提示词、人格、记忆与自主决策 |
| `cloud/agents/common/` | LangGraph 运行、检查点、动作账本、回复、过程事实、统一教导 |
| `cloud/{orchestrator,esp32,go2}/` | 服务入口、传输及真实设备执行适配 |
| `cloud/api/`、`cloud/cards/` | 认证、可信上下文、SSE、MQTT retained 设备卡注册表 |
| `cloud/{space,peers,personality,observability,storage}/` | 空间、协作、人格、追踪及原子存储 |
| `app/` | React 18 全局脚本 + Babel standalone，直接编辑 JSX，无构建 |
| `protocol/` | MQTT、Agent Card、标识协议 |

- MQTT 是所有智能体发现、能力与在线状态的唯一真相；HTTP Agent Card 是读取投影。ESP32 任务走 MQTT，Go2 数据面走 HTTP/SSE/WebRTC。同步设备调用不得阻塞 MQTT 事件循环，使用线程池与超时。
- `CardRegistry` 消费 retained manifest/card/status；管理员把整机加入固定 `main` 空间后，PWA 经 `/api/devices` 与 SSE 自动显示智能体。传感器只发布 state/event，不生成设备卡；不恢复手动订阅 UI 或文件型能力注册表。
- `agents/` 定义角色决策；设备服务保留传输、校验与执行适配。新增决策使用 LangChain/LangGraph，不新增自研 Agent 循环、工具解析、记忆编排或第二套运行时。传输辅助 `orchestrator/tools.py` 不承载业务决策。
- PWA 统一经 `ApiClient` / `CommandBus` 访问同源 API，不直连 MQTT 或轮询设备 REST 状态。新增脚本维护 `app/index.html` 加载顺序；头像仅投影呈现，不定义能力或物理状态。

## 权限、用户目标与真实回执

- API 校验 Cookie 会话和写请求 CSRF，构造 `actor/persist/space_id/requester` 及签名上下文；浏览器和模型不能声明权限、目标代次或运动特许。同伴消息不能授予权限。
- 新用户目标由 API 签发 `goal_revision`，经签名 MQTT/认证内部 HTTP 传播。模型返回、工具派发、发言发布前复核归属；取消旧等待并淘汰旧排队意图。明确续接沿原代次，失效运行不能恢复派发。
- 已完成或结果未知的物理动作保留原任务记录；超时按原 `task_id/request_id/run_id` 查询，不换 ID 重发。停止请求、传输成功和动作实测结果分别判定。
- Go2 新旧目标交接须等待旧任务清理及新鲜线速度/角速度停稳证据，按租约 generation 释放；未知停止不能因 TTL 到期解除占用。摇杆每个新手势绑定代次，旧帧不能影响新控制。
- 人员写入须管理员与有效腾讯云配置，开关为 `SPACE_FACE_ENROLLMENT_ENABLED`，兼容旧开关且缺省开启。本地仅存人员元数据；人物出现不等于可信身份，旧身份与历史记忆不能证明当前主人在场。

## Graph、教导与公开过程

- 三角色使用 System 职责、User 原话和原生 Tool 真实回执。多个工具按模型返回顺序串行执行；失败使本批剩余调用失效，执行中、超时或未知结果暂停后续动作。
- `accepted/ok` 按动作成功处理，但不伪造姿态或实测事实；Graph 将接受结果投影为 `result=ok`。`final=true` 清空后续调用并收尾；单个/批次末动作成功默认终结，中间动作与查询默认继续，需要继续原目标时显式 `final=false`。先剥离 `final` 再校验业务参数，收尾仅允许无工具回复。
- 一次性操作由服务端 `once_per_run_tools` 声明：成功或待确认后移出模型工具；重复同参复用原回执、不新增步骤，未知结果不换 ID 重发。真实失败允许业务重试，新目标不继承旧成功；查询和相对运动等可重复操作不使用该声明。
- 追问使用 LangGraph interrupt 和原运行恢复，保留原始目标与回答，不重做已完成动作。普通聊天复用原决策最终发言；自主结果由原角色 Graph 的结果分支处理，不另调人格包装或进展解说模型。
- 统一教导走 `agents/common/training/`：Card 派生能力 → 草案 → 管理员确认 → 执行器加载。逐步复核确切 ID/版本与权限；保存、加载和物理完成分别报告。本次覆盖不改长期记录；不恢复旧 `/api/teachings`、binding、`/api/rules` 或自动启用旧数据。
- 过程事实统一用 `common/progress_facts.py`，保留执行者、目标、父子运行和步骤身份。理由只引用当次可信观察；可选说明缺失/校验失败不阻断业务，必要执行证据仍严格校验。PWA 仅按身份和因果引用合并，不从相邻消息、阶段或模型文字补理由。

## ESP32 与灯

- `UNIT_CONFIGS` 声明引脚、探测策略及智能体 manifest。`probe.py` / `agent_manifest.py` 通用处理，不硬编码单元或 GPIO；新硬件仍需对应驱动、主循环实例和接线。
- BSM 只驱动硬件并抛事件，ISM 只转换状态，TriggerMap 是 BSM↔ISM↔MQTT 唯一接线点。教导与自主规则在云端；当前无离线自动控灯，不借修改接线恢复此功能。
- 使用 `ujson`、同步 tick 主循环、`ticks_diff()`；不引入 asyncio，不用直接减法处理 ticks 溢出。控制内存，探测结束释放引脚；JSON state/event 带 `unit_id` 和 `clock.unix_time()` 的 `ts`，校时沿用现有入口。
- 灯只有 `OFF/ON/BLINKING`，失败用任务错误报告，不新增 ERROR 状态。闪烁默认持续到开、关或调色命令，明确指定次数才有限闪烁；参数和输出上限沿用 `lamp_contract.py` / `config.py`。
- 默认 `manual`；`reactive` 结合新鲜感知决策，不写颜色硬编码规则表。用户/编排器控灯让位 300 秒，同伴控灯让位 60 秒；灯自己的自主/游戏动作不触发用户让位。物理读数与云端 control 投影分开合并，后者不能刷新物理时间。
- 接线以 `config.py` 和实际 `PRESENCE` 为准：灯 GPIO25、光线 GPIO34、声音 GPIO32；红外 GPIO19 记录为未接。光线/红外分别 ADC/数字探测，声音/灯使用 flag，蜂鸣器无配置条目。

## Go2 状态与运动

- 静态状态与转换仅由 Card `state_machine.regions` 声明，实时门禁来自执行适配。MQTT status 只表示服务在线，连接看 WebRTC，姿态看真实遥测与 `body_confirmation`，未观测为 null。模式不等于 life_state；休息不自动趴下，唤醒不自动起立或恢复探索。同 epoch 按 revision 合并，拒绝退役 epoch 迟到事件。
- 自由探索实际启动后 `life_state=explore` 覆盖轮次等待；互动接管为 play，仅探索许可仍有效时恢复 explore。空 `life_task_id` 不是可完成任务令牌，旧任务结束不能复活已停止的探索。
- 每次连接首次运动准备识别 MotionSwitcher：保留 mcf/normal；MCF 身体就绪后确认 ClassicWalk(true)/2049，normal 不发送该接口。每连接确认一次，摇杆与任务共用入口；步态设置不产生位移、不确认姿态，失败保留停止能力。
- 位移/转向共用 Navigator，摇杆与 Navigator 共用 `go2/motion.py`。任务平移和原地转向分开，切轴先确认停稳；普通导航先朝路线前进，显式相对移动沿指定身体轴，任务不混发多个非零 x/y/z。摇杆保留明确组合输入。
- 目标和经过加速度约束的实际速度都须检查最新共享地图。停止、取消、障碍、定位或权限丢失立即绕过平滑；重复定位包不续 Move 或原 0.3 秒看门狗，恢复后从零起步。后台重规划不得阻塞速度循环，过期结果不能恢复撤销路线，地图保留原观测时间及 2 秒有效期。
- 普通坐标位置容差 0.3 米，找物观察点 0.15 米，最终朝向 10 度；显式相对转向 0.05 弧度，明确更小位置容差仍生效。纠偏与验收使用同一容差，到点不替代视觉复核或真实停稳。速度更新目标间隔 50 毫秒；软件默认值/模拟通过不代替真机校准。
- 推物的观察、接近、推送由原 Go2 Graph 显式选择，执行器不自动追加共线站位或换位。绑定原货物、目的地和首次定位来源；视觉降级仍检查自身定位与避障，不设固定步长/里程/段数上限，保留用户明确距离和任务期限。
- 接触推送前读取机载避障原值，关闭并读回后仅该前推步骤跳过云端障碍拦截，结束/取消先停止再恢复；绑定原连接、目标代次和租约，不改共享地图。关闭未确认不推，恢复未知保留限制。推送尝试不证明货物送达；`already_near_destination` 只看本单首次观察并明确未移动。

## 活动与同伴游戏

- 活动由用户请求启动，API 发布签名 retained 当前快照；无默认 TTL。新用户介入使旧活动失效，只撤销其持有模式，记录停止真实结果；重启不重放动作。Go2 加入活动先由原 Graph 仲裁观察/交流/探索/等待，不强制先探索或用好奇心计数覆盖目标。
- 同伴消息校验签名、成员、期限和去重，收到、接受、开始、最终结果分别记录。普通回复沿可信 `conversation_id/reply_to`，显式发信与自动最终回复共用出口，不另起确认模型或无条件往返。
- 游戏由 `peers/games/` 注册表提供概览和当前阶段规则；所有模型决策与动作进入灯/Go2 自身 Graph，经 `role_graph.py` 接入。游戏层只处理协议、约定及结果，不另建模型 Graph，不直调物理执行器或重复维护运动算法。
- 内部错误、重试、工具回执标记 `context_scope=process`，仅作过程展示；协作子任务只共享一次最终 task_result，必要问题/约定才发同伴消息。中间决策允许静默，reason 不充当 speech，结构化 action 不充当真实回执。
- 邀请 ID 是 `game_instance_id`。先记录 game_closing 撤销普通动作/发言资格，双方原 Graph 回应整局真实结果，执行归属方仅发布一次 game_ended；不能从单个传输回执推断整局结束。迟到回执留在原任务，旧局退出当前上下文；再次玩须新邀请并接受，停止未确认不能恢复移动。

## 文件管理与验证

- 跟踪代码、有效测试、示例配置与 `protocol/`；`docs/`、`output/`、`.superpowers/` 属本地过程资料，不作为当前运行契约。新测试不应被忽略；过时测试按现行契约移除，不为旧断言恢复退役接口。
- 运行资产不得提交、清空或在验证中覆盖：共享检查点/索引/账本在 `cloud/data/`，教导在 `cloud/agents/data/training/`；设备在 `cloud/{esp32,go2,orchestrator}/data/`；空间数据在 `cloud/space/data/` 及 `cloud/space/{space,device_codes}.json`。具体环境覆盖见 README，测试全部使用临时目录或 fixture。
- `logs/broker/mosquitto.db` 当前是持久数据，不按日志清理；改目录须正常停 Broker 并迁移原数据。密钥只在本机配置，不读取或输出；Git clone 不替代运行资产及部署配置备份。
- Python 使用 `uv run python` / `uv run python -m pytest`，不手动激活 venv。仅跑相关验证，必要时全量：`uv run python -m pytest cloud edge -o 'python_files=test_*.py *_test.py *_checks.py' -q`。前端回归按 `scripts/check_*.cjs` 说明准备 Node/Chrome/React，不引入第二套构建链。
- 文档修改检查路径、命令及 `git diff --check`，不为此重启服务或触发硬件。运行排查用 `make ps`、`make logs`、`make trace-task ID=<session_id>`、`make errors`。模拟、真实模型和真机验收分别报告，不引用历史报告冒充当前通过。
