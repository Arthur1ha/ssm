# SSM — 智能系统网格（Smart System Mesh）

分布式多智能体 IoT 系统：以 MQTT 为统一**控制总线**（发现、在线状态、能力声明 card），**数据面按设备本性选传输**（ESP32 走 MQTT、Go2 走 HTTP/SSE）。连接 ESP32 边缘设备、云端 AI 智能体（管家 / 灯 / Go2 机器狗）和手机 PWA。设备通过 retained manifest/card 被发现，能力由 A2A Agent Card 描述；管理员将发现的设备加入固定空间 `main` 后，PWA 自动同步其状态与能力。

本文说明当前代码与部署入口；历史计划及验收记录不代表当前测试结果。依赖以 [pyproject.toml](pyproject.toml) 和 `uv.lock` 为准，配置以 [cloud/.env.example](cloud/.env.example) 与 [edge/config.py](edge/config.py) 为准。

## 系统架构

```mermaid
flowchart LR
    PWA[手机 PWA] <-->|同源 REST / SSE| API["FastAPI :8082<br/>灯 / Go2 服务与 Graph"]
    API <-->|签名意图 / 反馈 / 发现 / 同伴消息| MQTT[Mosquitto TCP :1883]
    MQTT <-->|意图 / 反馈 / 发现 / 同伴消息| Steward["独立编排器进程<br/>管家 Graph"]
    MQTT <-->|manifest / state / event / task / result| ESP["ESP32<br/>BSM / ISM / TriggerMap"]
    Steward <-->|内部 HTTP 语义任务 / Go2 调用| API
    API <-->|WebRTC 视频 / 遥测 / 设备动作| Go2[Go2 机器狗]
```

**控制优先级**：用户指令优先于自主行为；灯通过让位窗口、Go2 通过运动租约与中断尊重用户介入。当前固件没有离线自动控灯规则。

云端有三个 LLM 智能体，模型决策分别位于 `cloud/agents/{steward,lamp,go2}/graph.py`：

- **编排器**（`cloud/orchestrator/`，独立进程）：订阅用户意图，经管家 Graph 统筹全局，MQTT 派发到 ESP32、HTTP 委托到 Go2。
- **ESP32 桌面智能体**（`cloud/esp32/service.py`）：灯 Graph 处理对话与传感器触发的自主决策。
- **Go2 机器狗智能体**（`cloud/go2/`）：Go2 Graph 处理对话与自主决策；设备层负责导航、视觉与 WebRTC。

三角色统一以 System 消息承载职责和表达规则，以 User 消息保留用户原话，以原生 Tool 消息提供真实结果。普通聊天的最终回复直接来自决策最后一轮；独立动作回执和自主行动按需生成回复。必要补充由 LangGraph interrupt 暂停与恢复，完成的动作不会因用户补充而重新派发。

管家、灯和 Go2 共用动作结果与任务终结契约：`accepted` 与 `ok` 均按动作成功处理，Graph 将接受结果投影为 `result=ok`。工具结果的 `final=true` 直接结束当前任务并丢弃后续调用，不再进入规划轮次。单个动作或整批最后一个动作成功后默认结束；明确还需观察并继续原目标时，工具调用须声明 `final=false`，查询默认继续。收尾可以生成一次无工具的自然语言回复。

用户可通过自然语言教导场景、动作与参数，例如保存当前灯光用于阅读。场景由模型语义理解，调用时按教导 ID 和版本读取保存参数，支持本次局部覆盖与长期追改。Go2 的普通人物出现教导在待命和自由探索下都可触发，无需身份识别；带主人条件的教导另行校验可信身份。状态图连线展示场景、动作和具体参数。

也可以教设备怎样表达情绪，例如对 Go2 说“以后你开心就做开心动作”，或对灯说“以后你开心就闪三下”。确认教导后，普通对话或自主 Graph 决定表达 `happy` 时，可用同一 `emotion` 驱动头像表情并通过原执行链应用对应教导。没有映射时只显示表情；动作仍受模式、权限和真实回执约束。灯仅在教导明确次数时有限闪烁。工具已结束后的收尾情绪不补发动作，刷新或重连也不触发设备。详见[情绪与物理表达契约](protocol/topics.md#回复情绪与教导的物理表达)。

## ESP32 硬件与探测

| 硬件 | GPIO | 模式 |
|------|------|------|
| WS2812 灯环（数据线） | GPIO25 | 单总线 |
| 光线传感器 | GPIO34 | 模拟（ADC1） |
| 声音传感器 | GPIO32 | 数字，上升沿触发 |
| 红外传感器 | GPIO19 | 已配置，当前接线记录为未接；以启动探测结果为准 |

`edge/probe.py` 按 `UNIT_CONFIGS` 执行探测：光线使用 ADC，红外使用数字输入；声音传感器为开漏输出，当前与灯一样使用 `flag` 显式启用。缺席传感器不发 `state/event`；只有灯发布智能体 manifest，传感器不生成设备卡。灯环为 16 颗 WS2812，电池供电低亮测试档的默认开灯亮度为 `DEFAULT_ON_BRIGHTNESS=30`（0–255）；物理输出峰值上限为 `WS2812_MAX_VAL=30`。该输出上限覆盖常亮、显式调色和闪烁；消息中的 RGB/亮度仍表示命令参数。超限时 RGB 同比例缩小并取整，避免逐通道截断造成偏色；低亮度下颜色仍受整数精度限制，达到输出上限后继续提高亮度参数不会增加物理亮度。

## 各组件说明

### ESP32 边缘端（`edge/`）

| 文件 | 职责 |
|------|------|
| `boot.py` | 启动时连接 WiFi |
| `config.py` | 引脚、MQTT 地址、时序常量、**UNIT_CONFIGS**（unit 注册表） |
| `local_config.py` | 本机 WiFi/MQTT 密钥（从 example 复制，git 忽略） |
| `probe.py` | 按注册表执行 ADC / 数字探测或读取显式 flag |
| `bsm.py` | 行为状态机——GPIO/PWM/ADC 驱动，事件回调 |
| `ism.py` | 接口状态机——仅处理状态转换，不涉及硬件 |
| `lamp_contract.py` | 当前灯固件参数和能力契约，与云端工具及 Card 共用 |
| `trigger_map.py` | BSM ↔ ISM ↔ MQTT 接线（唯一耦合点） |
| `agent_manifest.py` | 为已探测到的智能体发布 retained manifest，并清理旧传感器 manifest |
| `mqtt_client.py` | MQTT 封装，支持自动重连和 LWT |
| `clock.py` | NTP 校时、Unix 时间换算与重试 |
| `main.py` | 主循环入口 |

传感器独立发布 `state/event`，供云端灯 Graph 使用。当前固件只订阅灯任务主题；云端离线时硬件 tick 继续运行，不会根据传感器新增自动控灯动作。

### 云端服务（`cloud/`）

环境变量统一存放在 `cloud/.env`（见 `cloud/.env.example`），模型构造统一在 `cloud/agents/common/models.py`。使用兼容 OpenAI 的模型服务，提供方与模型名由部署配置决定：

| 配置 | 用途 |
|------|------|
| `OPENAI_BASE_URL`、`OPENAI_API_KEY` | 文字推理与视觉识别共用的服务地址和凭据 |
| `TEXT_MODEL` | 全系统共用的文字推理模型，管家、灯与机器狗统一使用；代码默认 `deepseek-v4.1-flash` |
| `VISION_MODEL` | 全系统共用的视觉识别模型，代码默认 `glm-5.3-flashx`；模型 ID 以所用服务为准 |

灯与 Go2 的结构化决策沿用 LangChain 工具调用与 schema 校验，允许模型服务自动选择工具，不附加强制指定函数的 `tool_choice`，以兼容当前模型服务。模型请求失败或输出无效时不执行动作；活动与同伴仲裁错误写入 `model.error` 日志，不能把技术失败当成设备自主拒绝的理由。

Go2 身体动作在关节反馈可用时，以“本次动作变化 + 稳定目标姿态”确认完成，手动按钮与对话共用该判定。RPC 回包缺失只记录通信诊断，遥测确认后立即返回成功；较长动作通过已有任务查询返回结果，不重复发指令。没有动作证据时不会因站稳或等待超时而报成功。移动、转向和导航仍分别核对距离、角度和到达证据。

Go2 本地人物/人脸检测使用 OpenCV HOG/Haar；场景理解、目标搜索和视觉导航结合视觉语言模型、RGB/LiDAR 与里程计反馈。当前依赖不包含 YOLO/ultralytics。

视觉定位不要求相机、里程计和雷达的接收时间差小于 250 毫秒；各数据流独立到达，时间差本身不阻断任务。仍校验采样数据存在、定位会话与坐标系一致、画面新鲜，以及检测期间实际姿态稳定；移动仍经过导航与避障检查。

视觉目标导航统一由 `go2_navigate_to_visible_target` 先尝试目标定位和导航。地图不可用、自身定位不可用或目标雷达定位失败时，确认停稳后自动复用 `go2_find_object` 分段观察接近；公开入口不先执行内部重观察，避免把原始失败覆盖成另一种错误。导航已终结的地图/定位失败也可转找物，已完成的动作回执保留在 `initial_navigation`，不重放。 切换时记录已注册的 `navigation.fallback` 事件，包含原始原因与找物任务 ID，可据此追踪实际是否进入找物。目标未识别、视觉结果格式错误、动作失败、权限异常、取消、超时或运动结果未知不转找物。切换保留原目标、权限、定位会话、任务关联和剩余期限；整个工具沿用找物工具的执行窗口，精确导航阶段最多使用原有的 135 秒。只有 `outcome=nearby`、`found=true` 且 `stop.result=ok` 才确认已在附近，不伪装为精确测距成功。显式设置 `max_advance_m` 时不转为可能超出距离限制的完整搜索。普通对话、公开视觉导航技能和找颜色游戏共用此入口；猜拳攻灯直接使用找物流程。

Go2 人脸身份使用腾讯云 IAI 3.0 人员库。部署时在 `cloud/.env` 配置
`TENCENTCLOUD_SECRET_ID`、`TENCENTCLOUD_SECRET_KEY`、`TENCENTCLOUD_REGION` 与
`TENCENT_FACE_GROUP_ID`；人员库必须已存在且 `FaceModelVersion=3.0`。识别由
`GO2_FACE_RECOGNITION_ENABLED` 控制。管理员经 `/api/people` 从手机照片登记或删除人员；写入开关为
`SPACE_FACE_ENROLLMENT_ENABLED`，兼容回退到 `GO2_FACE_ENROLLMENT_ENABLED`，代码缺省为 `true`。
未配置服务凭据时人员库不可写；需要禁用写入时显式设为 `false`。

身份匹配固定以 80 分为阈值：达到 80 分且 PersonId 对应本地已登记人员时立即确认；
低于 80 分、无匹配或本地没有该人员时返回 `status=unknown`、`name=陌生人`、`role=stranger`。
不再配置接受分/候选分，也不等待两次匹配。自动识别先要求本地检测到恰好一张人脸，
且原图脸框短边至少 80 像素；主动请求识别时，无人脸、图像不合格和服务失败仍分别报告错误，不能据此判断陌生人。

Go2 视频的采集与 JPEG 编码分离，单个待处理槽只保留最新帧，编码上限 10 帧/秒。
识别与取证保留接收帧原尺寸、JPEG 质量 90；手机优先通过同源 MJPEG 长连接接收共享预览，
预览最多 640 像素宽、JPEG 质量 70，失败时回退到 `/api/go2/video/snapshot?preview=true`。
快照默认仍返回原尺寸图；视频展示只接受当前连接中不超过 3 秒的帧，过期或视频轨道结束时拒绝旧图。
隐藏、卸载或断连会取消浏览器媒体请求，地图请求有超时，地图绘制与压缩在后台线程运行。
后台检测约每秒取一次新帧：人体检测等比缩至最多 320 像素宽，人脸检测等比缩至最多 640 像素宽，
身份识别从原尺寸图裁脸。检测结果沿用真实采集时间和源帧编号；重复帧不产生新观察，
超过 5 秒的帧不能用于当前感知。

Go2 远程登录与信令的同步网络调用在后台执行，单次信令等待上限为 15 秒。
上层超时后，底层 SDK 请求仍可能继续到其自身超时；这期间重试会报告信令仍在进行，
不会排队创建更多连接或阻塞 API，晚到结果也不会安装新连接。媒体故障不会自动重发设备动作。

身份结果最多有效 5 秒，`GO2_FACE_RECHECK_S` 取 1~5 秒（旧的更大值按 5 秒处理）。
人脸消失、变为多人、位置明显不连续或相机会话变化会撤销旧身份；连续单脸也须定期重新匹配，
脸框连续不等于身份已被重新验证。聊天与自主社交只读取新鲜身份投影，不从历史记忆推断当前主人。
腾讯云检查/搜索失败报告错误，保留识别开关，至少间隔 5 秒再尝试恢复；重连会重新检查。
人员出现教导可在最多 5 秒内等待新鲜身体遥测；用户接管、离开画面、事件过期或设备正被占用时丢弃等待，
不排队补发旧物理动作，实际动作仍走原教导版本、权限和完成回执校验。

本地只保存 `PersonId`、姓名、关系和人脸数量到
`cloud/space/data/people.json`（可用 `SPACE_DATA_DIR` 覆盖目录，旧 Go2 人员文件仅用于兼容读取），不在本地人员库保存原始照片或 embedding；照片会上传腾讯云用于登记。

#### API（`cloud/api/`，FastAPI，端口 8082）

| 接口 | 说明 |
|------|------|
| `POST /api/admin/session`、`POST /api/visitor/session` | 创建管理员会话与访客邀请兑换 |
| `GET /api/me`、`DELETE /api/session` | 当前会话与退出清理 |
| `GET  /api/devices` | 固定 `main` 空间设备；已配置的离线设备仍返回 |
| `GET  /api/devices/{unit_id}/agent` | **A2A Agent Card**：机器可读的能力描述（数据取自 `cloud/cards/`） |
| `POST /api/intents`、`POST /api/commands/{unit_id}` | 认证后的统一手机命令入口 |
| `GET /api/events` | 按会话过滤的反馈与设备状态 SSE |
| `POST /api/emergency-stop` | 管理员和体验者均可用的全局安全停止 |
| `/api/training`、`/api/training/proposals` | 管理员培养记录、修改草案与用户确认；设备页培养入口查看和追改 |
| `GET /api/devices/{unit_id}/training` | 可训练转换、教导定义和动作参数 |
| `/api/admin/*` | 固定空间和访客邀请管理 |
| `GET /api/activity/current`、`GET /api/activity/{activity_id}/events` | 当前活动、历史元数据与时间线 |
| `POST /api/activity/start`、`POST /api/activity/end` | 启动与结束空间活动，要求有效会话及 CSRF |
| `/api/people`、`/api/agents/{agent_id}/personality` | 管理员人员库、人格配置 |
| `GET/PUT /api/esp32/autonomy` | 灯自主模式；修改要求管理员身份及 CSRF |
| `/api/go2/*` | Go2 数据面；读接口检查会话/权限，写接口要求内部服务身份与可信执行上下文 |

`api/main.py` 同时承载一个 ESP32 MQTT 桥，订阅 `manifest/state/event/result` 并喂给 ESP32 桌面智能体。

#### 编排器（`cloud/orchestrator/`，独立进程）

订阅 `ssm/intent/+`，调用 `cloud/agents/steward/graph.py` 的 LangGraph 逐步任务图，根据工具结果的 `final` 明确结束或继续规划。三角色共用 `cloud/agents/common/` 的文件检查点、动作账本和事件投影。

同一智能体、同一会话的对话沿用检查点中的 `messages` 通道，保留最近约 8 轮问答（最多 16 条消息，并受约 3000 token 预算限制）。任务等待澄清时，每次追问和用户回答也写入该通道；恢复时按原顺序提供给模型，不用最新一句覆盖先前回答，也不重复加入原始请求。旧会话历史用于理解偏好和指代，当前任务的澄清用于继续本次决策；物理执行仍以动作账本和真实回执为准。

| 文件 | 职责 |
|------|------|
| `main.py` | MQTT 事件循环：意图 → 逐步任务循环 |
| `../agents/steward/graph.py` | 逐轮规划、派发、核对结果并生成管家回复 |
| `tools.py` | MQTT/HTTP 派发与反馈辅助 |
| `shared_state.py` | 线程安全 MQTT 传感器/执行器快照与可信会话元数据 |
| `trusted_context.py` | 签名意图、同伴请求与会话清理校验 |
| `card.py` | 管家公开能力卡 |

> 设备能力卡注册表见 `cloud/cards/`（`registry.py` 订阅 card/manifest/status，是 A2A 单一真相）。

#### 决策、设备服务与共享能力

| 目录 | 职责 |
|------|------|
| `agents/` | 三角色的 Graph、提示词、人格、记忆和自主工作流；`common/training/` 提供统一教导 |
| `esp32/` | 灯服务、自主模式、传感器事实、动作校验、任务去重与 MQTT 回执 |
| `go2/` | HTTP 服务、WebRTC、视觉、导航、执行校验与自主事件调度；调度器调用 `agents/go2/workflows/` 的 Graph |
| `personality/` | 三角色共用的人格存储、版本校验和名称管理；具体人格在各自 `agents/*/personality/` |
| `peers/` | 同伴发现、签名消息、技能请求、协作权限、取消和互动游戏 |
| `space/` | 空间成员、执行身份、用户要求、活动时间线和人员库 |
| `observability/` | 结构化日志、任务关联事件与脱敏 |
| `storage/` | 带锁、原子化的 JSON 文件存储 |

空间成员配置保存在 `cloud/space/space.json`，设备能力与在线状态仍来自 MQTT retained 消息。当前没有文件型设备能力注册表；教导通过 `/api/training` 管理，旧 `/api/rules` 与 `/api/teachings` 已退役。详细文件说明见 [云端智能体目录与设备互通](docs/architecture/agent-directory-and-peer-messaging.md)。

空间主页与活动时间线在消息更新时保留当前阅读位置；新动态通过“回到最新”按钮手动定位，不自动拉到底部。

#### 状态、教导与协作边界

- **灯**只有 `OFF`、`ON`、`BLINKING` 三个物理状态。默认 `manual`，显式开启 `reactive` 后才自主调光；用户命令使自主层让位 300 秒，同伴控灯使用独立的 60 秒让位窗口。普通“闪烁”按钮默认持续闪烁，指定次数时才有限闪烁。
- **Go2**使用 connection / activity / life / body 分层状态，`autonomy_mode=manual|free_explore` 是配置。自由探索启动后，“探索”持续覆盖观察、规划和轮次间等待，具体进展通过对话展示；互动接管时显示“互动”，结束且探索许可仍有效时恢复“探索”。MQTT `status` 表示云端 Go2 服务在线，实际 WebRTC 连接看 `state.connected`；在线不等于已连接或已站立。RPC `accepted` 在任务层按动作成功处理；姿态、运动与停止字段仍由遥测更新，休息不自动趴下，唤醒不自动起立或恢复探索。身体遥测不设有效期，已确认姿态不会仅因超过 2 秒未更新就禁止移动、导航或找物；断连、明确电机故障和动作未确认仍按真实状态处理。
- **统一教导**从 Card 的转换与动作参数生成目录，管理员自然语言提出草案并确认，执行时重读指定教导 ID、版本及约束。单动作、多动作、事件教导与场景参数复用现有执行链；保存、执行器已加载和物理动作完成分别展示。人物出现教导可在待命和自由探索触发；带主人条件的教导必须使用可信身份观察。
- **空间活动**保存用户原话、设备实际加入模式、消息、观察、动作及回执。活动通过 API 或管家工具启动，为灯选择 `reactive`、Go2 选择 `free_explore`；Go2 先将当前活动目标交给既有 Graph 判断，选择观察、交流、探索或等待，不因进入活动就必做一整轮探索。活动内不凭好奇心计数自动覆盖用户目标，直接开启的自由探索仍沿用原探索入口。用户再次介入会结束当前活动，切回活动持有设备的 `manual` 并记录停止结果。活动没有默认到期时间，重启恢复不重放历史动作。
- **同伴协作**区分定向消息与单技能调用。消息受签名、成员身份、期限和去重校验；发出邀请不表示对方接受或动作完成。`color_hunt` 由模型结合新鲜观察决定是否邀请、选色与找物，不按固定延时或预设剧情执行。需要决策依据时，工具校验 `decision_reason/evidence_refs`，时间线保存当时的来源与摘要。

互动游戏在 `cloud/peers/games/` 按游戏分文件管理：每个文件定义该游戏的规则、玩法、策略说明、可用条件与执行入口，统一注册表负责汇总。找颜色、猜拳攻灯和配送均声明双向邀请，发起方不决定角色：灯和狗任意一方都可以提议，另一方自主接受、拒绝或延后。只有双方同处有效活动、在线且没有正在执行或等待确认的任务时才可邀请和开局；Go2 在探索轮次之间无具体任务、无导航或运动时可参与，休息、手动控制、动作未确认或等待用户补充时不可开局。灯的常亮、熄灭或持续闪烁本身不等于云端任务忙碌，实际会话、教导和物理回执等待分别检查。

自主社交没有固定发送间隔、次数配额或同话题冷却；双方进入空闲段以及新观察可提供一次重新判断的机会，现有 Graph 结合人格 traits、当前环境、任务及近期互动决定邀请、交流或保持安静。空闲不代表愿意接受；同一空闲段不会按时间反复催发邀请，同一对设备的待处理邀请也会阻止交叉发起第二局。找颜色支持灯邀请狗和狗邀请灯两个执行方向；双方同意后仍由灯依据真实观察选色并确认物理回执、狗寻找并以停稳和视觉复核确认结果。邀请气泡只简短说明邀请和必要玩法，决策原因及引用通过“查看进展”按需查看，不自动拼入对话正文。

新活动原话会提供一次独立的社交评估机会，成员加入结果提交后唤醒灯的现有工作队列；即使暂不满足游戏开局条件，也能由模型判断交流、询问或等待。任务占用和未确认回执仍阻止开局，不能阻止对新请求的评估。该机会按活动去重，重启不重放旧要求。

活动工作上下文按活动归属与本次运行代次隔离：Go2 停止或重新启动自主循环时清空上一运行的目标、决策、工具结果与观察；同活动当前运行的真实结果仍用于逐步规划。旧任务晚到的结果只保留在原任务检查点与设备账本中，不写回新运行的上下文或时间线。关联同伴回复继承原消息的活动归属；原活动结束后，旧同伴任务不能因为新活动重新启用了 `reactive` 而继续发信或派发新动作。当前连接、姿态等真实设备状态与持久动作去重账本独立保留，不能通过清空工作上下文重放未确认动作。

同伴已发来的消息优先于另开自主话题；回复沿用原 `conversation_id/reply_to`，同一处理轮只保留一个对外回复出口。普通回复可交回现有角色 Graph 判断是否需要续谈，消息收讫不再由独立模型补一句对话。已注册游戏须通过结构化邀请进入对应执行流程，普通聊天中的出拳描述、对方发言或口头判胜不能代替双方约定、锁定选择和真实动作回执。管家执行收尾聚焦本轮目标及未确认项，历史对话仅帮助理解指代与偏好，不自动续做或总结已切换话题的旧请求。

新增游戏时：

1. 在 `cloud/peers/games/<game_id>.py` 定义 `GAME = Game(...)`，参考 `color_hunt.py`。填写 `description`（玩法简介）、`rules`（规则与完成判据）、`strategy`（适合邀请/接受的情境），以及 `participants`（参与双方）、`available` 和异步 `execute`。`bidirectional=True` 允许双方互邀，`requires_idle=True` 在邀请和接受开局前核验真实任务占用；未指定时保持已有单向游戏兼容。
2. 在 `cloud/peers/games/registry.py` 导入该 `GAME` 并加入 `GAMES` 的汇总项。规则和策略随可玩列表及邀请上下文进入现有 Graph，无需逐个修改角色提示词。
3. 按需提供 `on_invitation`（灯收到游戏邀请的处理入口）及 `on_acceptance` 准备回调；问答和意愿由既有角色 Graph 生成，规则不复制到角色提示词，不把狗的邀请重写成灯的固定口吻。执行复用已有 Graph、设备工具与真实回执，保留原邀请 ID 去重及结果关联。补充该玩法的模拟回归，勿用现有运行资产验证。

猜拳攻灯 `rps_siege` 的规则和执行入口在 [`cloud/peers/games/rps_siege.py`](cloud/peers/games/rps_siege.py)，角色选择在 `rps_decisions.py`，双向邀请适配在 `rps_lifecycle.py`。流程为“接受参与 → 公开双方映射 → 确认规则 → 认灯定位 → 秘密选拳并同时展示 → 判胜负 → 兑现奖惩”。接受邀请只表示愿意进入准备，双方仍可基于意愿和当前任务拒绝或暂缓。双方先选择石头／剪刀／布的表达：灯选三个可区分的亮灯颜色并提供相符的颜色名称，狗从当前动作契约中选三个不同的动作。执行链逐项公布完整对应关系和规则提议，再由双方分别确认；映射是公开规则，本轮选拳才保密。拒绝时保留提议，不亮识别色、不观察或出拳。

规则确认后，灯自主选一个识别色，真实开灯、设色回执确认后告诉狗。用户提供的外形线索是“蓝紫色球体”，会把灯放在狗的视野内；提示词把这些作为识别线索，不作为已经看见或确认位置的证据。狗读取设色确认后的新画面，综合球形、蓝紫色外观线索、实际发光、当前识别色与周边固定特征辨认。发光会改变外观颜色，相机呈色也可能不同，不能只因色调不完全匹配就排除候选，也不能仅凭颜色或同伴自述确认身份。观察不足或候选无法区分则停止出拳，保留已公布的规则，不安排开局搜索。定位后保留观察支持的位置、形状或固定标记，出拳变色不更换目标，也不改写已确认的映射。

一把定胜负，平局重来，不协商回合数或推进距离。双方各自秘密选拳并锁定后，并发派发灯变色与狗动作，不等待一方完成才启动另一方；网络与设备响应存在时延，不承诺物理上精确同步。仅当双方真实动作回执均确认后才判拳，一方失败或结果未知不判胜、不进入奖惩。用户介入时拦截过期模型决定、取消待处理展示并沿原执行链停止运动；旧物理动作不重放。

狗赢后调用 `go2_find_object`，沿可通行路径分段接近约定的圆球灯，每段停稳后重新观察；连续新鲜画面判断已在附近且确认停稳后，执行 `Hello` 挥手抬前腿，象征性按一下灯；等待真实动作完成反馈后再发送 MQTT 关灯命令，不要求实际碰灯。不要求雷达分离出灯的点簇或取得精确目标距离。关灯前核对找物结果为 `result=ok`、`outcome=nearby`、`found=true`、`stop.result=ok`，且目标描述与任务 ID 均属于本局；仅看见目标、推进了一段、停稳未确认或抬前腿动作未确认完成均不能关灯。灯赢则由狗从支持的动作中自主选择一次表演，例如跳舞，等待真实动作完成反馈。猜拳胜负与后续履约分别记录：导航、抬前腿、关灯或表演失败不会改判已经确认的赢家，也不能声称奖励已兑现。游戏期间灯暂停普通自主调光；结束后释放占用，确认关灯的结果保留到新的有效事件，旧观察或定时检查不能马上重新开灯。Go2 在活动与探索许可仍有效时恢复自主状态，并把正常完成的游戏结果交回既有活动 Graph 判断后续探索、观察或等待；用户接管后不强制恢复模式。失败、结果未知、用户介入、断连或定位会话改变会停止后续执行，不重放旧物理动作。

可在聊天中提出“让灯和狗玩一局猜拳攻灯”，沿用现有活动及同伴邀请入口；双方仍自主判断是否接受。游戏注册项的 `invitation_ttl_s` 默认 300 秒，猜拳攻灯为 900 秒，包含准备、平局重来、出拳与履约；关联接受和结果沿用原期限，普通消息保持原有限制。900 秒是执行截止时间，超时不改判胜负。模拟回归与真实模型、动作和导航的实机验收分别进行。

配送游戏 `push_delivery` 已接入可玩列表，规则与执行入口在 [`cloud/peers/games/push_delivery.py`](cloud/peers/games/push_delivery.py)，角色决策和双向邀请分别在 `delivery_decisions.py`、`delivery_lifecycle.py`。流程为：任一方自主邀请 → 对方接受并确认角色（灯负责指挥、Go2 负责找货与配送）→ 灯公布货物特征 → 狗自主搜索并按原任务回执核对货物 → 狗报告找到并等待目的地指令 → 灯根据新鲜观察选择目的地及本单指令色 → 双方确认本单货物、目的地与灯色 → 灯亮出颜色并核对真实回执 → 狗按同一订单逐步靠近、尝试用腿推送 → 同一货物到指定目的地附近、狗停稳且新鲜画面复核后报告大致送达。送达不等于签收。目的地可以是色块、小地面标记或物体标识；灯色可以表示双方明确约定的站点，不要求物体与灯同色。灯直接决定货物，不向狗征求选货意见；用户未指定时，依据真实观察优先选择离狗较近、容易接近、看起来方便推动且有一定体积的独立物品，地面色块、贴纸和站点标记留作目的地。观察提供相对远近、外形和通路线索，不保证物品一定能推动，也不设置固定距离、尺寸或物品种类。货物按特征搜索和核对，可以已经在狗附近，不要求固定摆放位置；邀请、选货、选区、问答及下一步行动由角色模型通过既有 LangGraph 机制决定，不预排固定对白或行动剧本。

**配送不再要求推板，当前开放的是受控步态接触推送尝试，实机效果待验收。** `go2_push_cargo` 根据当前视觉估算或显式请求距离执行前进，不设置固定步长，复用原有 Navigator、站姿校验、运动权限、任务账本和真实停稳；没有新增专用踢腿动作。靠近时使用完整障碍地图；进入接触阶段后，执行器仅为本次前进生成私有通路视图，从新雷达快照中重新关联视觉已确认的唯一货物，只允许接触这件货物的精确三维点。共享地图和普通导航保持完整，其他障碍以及货物前方扫掠空间仍受检查。货物丢失、与其他障碍混合、侧偏、定位或控制权变化即停止；许可不能复用到其他动作。代码不修改机载避障开关，不能假定机载避障一定会过滤当前运动接口。

推送适配在 `cloud/go2/navigation/cargo_push.py`，接触范围在 `cargo_contact.py`，配对感知在 `cloud/go2/perception/cargo.py`：真实雷达测量同一货物位置，视觉定位指定目的地在地面的位置；停稳后一张新鲜画面确认货物在其附近即可大致送达，不要求最小色块面积、完整区域边界或货物全覆盖。附近按画面中货物地面接触位置与目标区域或物体底部的间隔约一个货物可见宽度判断，是视觉粗略位置关系，不声称精确世界距离。机器人走动、画面框变化或普通导航成功均不能替代货物移动及送达证据。货物与目的地需可辨认，方向不合、通路受阻、货物丢失或反馈不足会如实结束，不盲推。游戏期间灯暂停普通自主调光；用户介入、灯色改变、断连、定位会话改变或整局 900 秒期限到达会中断，重启不重放旧订单。可在聊天中提出“让灯和狗玩指挥官与送货员”，也可在活动要求中描述实际货物与目的地。

先做基础推物测试时，可直接对狗说“只测试推一下眼前带白色图案的深色盒子，本次最多前进 0.15 米，不要求送到蓝标”。此时 `go2_push_cargo` 省略 `destination`，只识别并测量同一货物，不要求配送区；`progress=approached` 仅说明靠近，`progress=pushed` 才说明停稳前后测得可靠的货物位移，始终不声明送达。测试指定目的地时可以说“把这个盒子推到蓝色小方标附近”或“推到那个物体标识旁边”，无需把货物放进或完全覆盖标记。找物或导航作为复合任务的前置步骤时通过 `final=false` 继续原目标，不再强制结束整个对话任务。受控步态不保证所有物体都能推动；雷达无法分离低矮货物时可走视觉模型估距的推送尝试；仍无法核对同一货物或其他障碍阻塞时返回受阻。基础测试不能证明配送游戏已通过真机验收。

静态状态图以 `state_machine.regions` 为唯一拓扑，运行值从 state/SSE 合并；Go2 使用 `epoch/revision` 排序。灯的 `control_state` 与 `control_ts` 是云端控制投影，不能覆盖物理灯状态或刷新传感器事实。协议细节见 [Agent Card 契约](protocol/agent-card.md) 和 [MQTT topics](protocol/topics.md)。

配送的找货步骤额外核对同一货物的位置。雷达能关联货物时使用实测位置；雷达测距、货物关联或地面平面不可用时，视觉模型根据画面估算大致距离及建议前进距离，来源标记为 `visual_distance_estimate`，不冒充雷达测量。省略 `go2_push_cargo.distance_m` 时使用本次视觉建议，也可传入任意有限正数的请求距离；不默认或截断为 0.15/0.25 米。找物不再按 0.8 米或 5 秒切段，也不设置 20 段／20 米上限，停稳后再观察；Go2 对话不按 6 次动作／12 轮决策截断，仍有原任务期限。视觉路线沿完整障碍地图执行，不创建障碍豁免，结果区分转向、接近和尝试推送，不凭机器人移动声称货物移动。目标测距失败时导航可降级到同一找物 Graph；调用方明确提供的 `max_advance_m` 在降级后仍遵守。自身定位、避障地图完全缺失或控制权改变时仍停止；这不是纯视觉避障。

### 手机 PWA（`app/`）

无构建步骤的 React 应用（Babel standalone）。浏览器只连接 FastAPI：REST 负责登录、命令与管理，认证 SSE 负责实时设备状态和编排反馈；浏览器不加载 MQTT.js，也不持有 broker 写凭据。

系统只有一个固定空间 `main`。用户点击“创建空间”即可进入并管理教导、邀请与长期资产；体验者扫描邀请二维码获得短期 Cookie 会话，只能使用已有能力。界面形态为**单屏聊天中心 + hash 设备子页**，包含活动卡、详情时间线、设备状态图、培养抽屉和空间人员库；访客界面不会显示长期配置入口。

聊天直接保留原角色的发言和情绪，气泡不显示设备形象。管家、灯和 Go2 的开场白根据各自人格生成并经用户反馈修订，固定保存在前端；打开页面、刷新或修改人格不重新生成，名字沿用当前配置。普通闲聊没有过程面板，等待提示跟随本轮消息显示；有实际调用时，等待动画合并到对应过程的标题行，完成后不显示成功对号，失败、部分完成和待确认状态仍保留。主页输入框上方不再常驻展示“发送新指令将结束当前自由活动”的提示；用户新指令结束活动的行为不变。展开后按短句展示原有选择说明、引用观察、相关动作参数及真实回执，不遍历字段表，不显示任务编号、运行编号或内部状态码；不额外添加“选择说明”“当时设备反馈”等栏目文字或重复的成功段落。成功详情默认收起，执行中的调用和失败、部分完成、结果未确认保持可见；未记录的理由不补写，调用角色和目标设备分别标示。实时事件与历史补载按运行、步骤和源事件身份合并，不按相同文本或相邻时间推断重复与因果。已结束活动只用状态标签标示结束，保留必要结果和停止未确认原因；详情入口不显示记录数量。活动成员与设备页的形象继续由 `app/src/avatars/` 管理，开发预览见 `/avatar-preview.html`；形象只表达呈现状态，设备事实仍来自 Card 与实时反馈。已揭晓的猜拳手势保留，历史加载和重连不会重播动作动画。

活动成员以共享舞台展示：伙伴形象、名称与当前状态居中排列，不再使用独立设备卡片边框；伙伴区域仅展示信息，不触发页面跳转。参与情况汇总显示；离线、反馈过期、用户设置让位等提示仍跟随对应伙伴。最近出拳查看活动记录，舞台仅播放新鲜实时事件的表情与手势。

`app/serve.py` 为本地 JS/CSS 注入修改时间版本号，编辑后刷新即可；新增组件须同步 `app/index.html` 的脚本加载顺序。页面还从 CDN 加载 React / ReactDOM 18.3.1、Babel standalone。

## MQTT 消息协议

**完整定义以 `protocol/topics.md` 为单一真实来源**，下表为概览：

| 类型 | Topic | 保留 | 发布者 | 说明 |
|------|-------|------|--------|------|
| `manifest` | `ssm/agents/{unit_id}/manifest` | 是 | ESP32 | 设备能力声明，启动时发布；空 payload = 单元缺席移除 |
| `card` | `ssm/agents/{unit_id}/card` | 是 | Go2 服务 / 管家 | 完整 AgentCard JSON；管家只进入同伴视图 |
| `state` | `ssm/agents/{unit_id}/state` | 是 | ESP32 / Go2 服务 | 灯/传感器快照或 Go2 分层运行快照 |
| `event` | `ssm/agents/{unit_id}/event` | 否 | ESP32 | 传感器事件 |
| `status` | `ssm/agents/{device_id}/status` | 是 | ESP32 / Go2 服务 / 管家 | 原始文本 `online`/`offline`，配合 LWT |
| `location` | `ssm/agents/{device_id}/location` | 是 | ESP32 | 设备地理坐标（GCJ-02） |

**控制 / 编排 / 表达 topic**：

- `ssm/intent/{session_id}` —— FastAPI 验证会话后发布的内部意图（HMAC + 时间窗 + nonce，编排器拒绝篡改与重放）
- `ssm/task/{unit_id}/{task_id}` —— 任务下发（云端 → 设备）
- `ssm/result/{unit_id}/{task_id}` —— ESP32 任务结果（设备 → 云端，结果及错误含义以协议为准）
- `ssm/feedback/{session_id}` —— 编排器渐进式执行反馈
- `ssm/decision/active` —— 编排器兼容在线标记（`true`/`false`），当前固件不据此自动控灯
- `ssm/session/{session_id}/ended` —— 签名会话结束与资源清理
- `ssm/agents/{target_unit_id}/message` —— 签名同伴定向消息与接收/进度/结果
- `ssm/peer/{request|result|cancel}/{request_id}` —— 请求管家协调的签名同伴任务
- `ssm/space/main/constraints/current` —— 签名 retained 用户空间要求
- `ssm/space/main/activity/current` —— 签名 retained 当前活动快照

payload 依消息类型定义：设备 JSON 状态/事件带 `unit_id` 与 Unix 秒 `ts`；`status`、`decision/active` 是原始文本，撤销 manifest/card 使用空 payload；其他签名封套使用各自协议字段，不能套用统一 JSON 结构。

## 快速上手

### 0. 环境与配置

需要 Python ≥3.13、uv；Broker 需要 Mosquitto 和 `envsubst`。使用已有公网拓扑时还需要自行配置 nginx 与 Cloudflare Tunnel；这些服务不会由 `uv sync` 安装。

```bash
uv sync          # 安装所有依赖
cp -n cloud/.env.example cloud/.env   # 首次配置，保留已有文件
```

始终在项目根目录执行 uv 命令；Python 脚本用 `uv run python`，无需激活虚拟环境。启动前编辑 `cloud/.env`：

1. 配置模型服务地址、API key、文字推理模型 `TEXT_MODEL` 与视觉识别模型 `VISION_MODEL`；示例文件中的提供方和模型名需按实际服务调整。
2. 配置 MQTT 地址和凭据；`MQTT_API_*`、`MQTT_ORCHESTRATOR_*` 分别优先于通用 `MQTT_USER/PASSWORD`。示例包含专用用户名，必须在 Broker 创建对应账号，或留空以回退通用账号。
3. 为 API 与编排器设置同一个 `SSM_INTERNAL_SERVICE_TOKEN`，MQTT 签名默认由它派生。`SSM_SESSION_SECRET` 至少 32 字符才能跨 API 重启保留会话，否则使用临时密钥。
4. 使用 Go2 时补充 `GO2_EMAIL`、`GO2_PASSWORD`、`GO2_SERIAL` 与可选 `GO2_REGION`（默认 `cn`）；这些项目前需自行加入 `.env`。人脸服务按上文另行配置。

`infra/broker/passwd` 是部署机认证文件，不随仓库分发。首次可用 `mosquitto_passwd -c infra/broker/passwd ssm_api` 创建，再用不带 `-c` 的命令追加 `ssm_orchestrator`、`esp32_desk`，并将对应密码填入云端与边缘本地配置；已有密码文件不得用 `-c` 覆盖。

### 1. 启动云端服务

```bash
make broker        # 前台启动 MQTT Broker（TCP 1883）
make api           # 前台启动 FastAPI（端口 8082，代码与 cloud/.env 热重载）
make orchestrator  # 前台启动编排器（崩溃自动重启）
make pwa-bg        # 后台启动 PWA 文件服务（端口 8081）
make tunnel-bg     # 后台启动 Cloudflare Tunnel HTTPS 隧道
make tunnel-url    # 输出公网地址（https://ssm.eliottxu.top）
make ps            # 查看进程
make logs          # 查看最近会话和错误
```

前台服务分别在独立终端或 tmux 窗口运行。带 `-bg` 的目标会先停止匹配的旧进程再后台启动；重复调用会重启服务。`make api` 用于开发：监视 `cloud/` 下的 Python 代码与 `cloud/.env`，保存后自动重启 API；运行数据 JSON 和日志不会触发热重载。前台请求排空等待为 2 秒，日志同时写入 `logs/api.log`，Ctrl+C 后继续显示关闭过程。`make api-bg` 不启用热重载，请求排空等待为 10 秒，代码或 `.env` 修改后需手动重启。这些等待上限不包括应用清理和已运行的线程任务。编排器是独立进程，相关配置修改后仍需单独重启。已运行的旧进程不会因启动命令修改而更新，在 tmux 的 `api` 窗口停止旧进程后重新执行 `make api` 即可切换。`make stop` 停止 Makefile 管理的服务。

API 和 PWA 分别只监听 `127.0.0.1:8082`、`127.0.0.1:8081`，需经同源代理访问。`make tunnel-bg` 使用预先建立的名为 `ssm` 的 tunnel；`make tunnel-url` 只打印 Makefile 配置的地址，不会自动创建隧道或检查公网可达性。

### 按会话查看日志

使用 `make trace-task ID=<session_id>` 查看一个会话内灯、机器狗和编排器的步骤；也可按 `AGENT_ID=<agent>`、`RUN_ID=<run>` 或 `TASK_ID=<task>` 检索。加 `JSON=1` 可看脱敏后的完整字段，加 `FOLLOW=1` 可持续跟踪。默认输出包含模型输入输出、决策、工具参数、接收方与结果摘要。`make logs` 显示最近会话及错误摘要。在现有 tmux 会话内运行 `make tmux-windows` 会按名称增补 `session_trace`、`errors`、`mqtt-trace` 窗口；`make tmux-windows ID=<session_id>` 将任务窗口切到指定会话。也可从会话外设置 `SSM_TMUX_SESSION=<会话名>`；设置 `SERIAL_PORT=<设备端口>` 时增补 ESP32 串口窗口。重复执行不会重启 `api`、`orchestrator` 或 `broker` 服务窗口。

任务事件在首次发生时追加到 `logs/session-<session_id>.jsonl`，API 与编排器共享同一个会话文件；没有会话 ID 的自主任务写入 `logs/session-system.jsonl`。每条事件写完立即关闭文件，不会因进程启动产生空文件，文件删除后下一个事件会重新创建。启动时清理 14 天未更新的会话日志。Graph 检查点、运行索引与物理动作账本使用本地文件持久化；旧 `data/runs/` 记录已另行归档，不由新入口恢复。`make rotate-logs` 对 API、编排器等 stdout 和 Broker 日志执行独立轮转。API 普通日志写入 `api.log`（包含云端灯和机器狗），编排器普通日志写入 `orchestrator.log`；Broker/串口原始日志各自保留。

默认运行数据：`cloud/agents/data/{checkpoints,run_index,effects,training}/` 保存共享检查点、运行索引、动作账本与教导；灯与 Go2 各自在 `cloud/esp32/data/`、`cloud/go2/data/` 保存设备任务/记忆；`cloud/space/data/` 保存活动和人员元数据。这些是运行资产，不能在验证中清空或覆盖。未知物理结果按原 ID 查询，不自动重发。

### 2. 配置并上传 ESP32

先刷入适用 ESP32 的 MicroPython 固件，并确认设备有 `umqtt.simple`（不保证随固件内置）。复制 `edge/local_config.example.py` 为 git 忽略的 `edge/local_config.py`，填入 WiFi 与 Broker 凭据，再通过 mpremote 上传（需在本机安装该工具）：

```bash
cd edge
mpremote connect <端口> cp boot.py clock.py lamp_contract.py config.py local_config.py probe.py ism.py bsm.py \
  mqtt_client.py trigger_map.py agent_manifest.py main.py :
mpremote connect <端口> reset
```

上传后串口应输出 `[Clock] Unix time synced: ...` 和 `[Probe] {...}`。若 NTP 暂不可用，设备继续运行并每分钟重试；校时成功后会重新发布 retained 状态。

仅更新上述默认亮度及输出上限时，上传 `edge/config.py` 和 `edge/lamp_contract.py` 到 ESP32 根目录并重启即可；此次 RGB 等比例限幅修复还需上传 `edge/bsm.py`，仅更新配置不会改变旧驱动的逐通道截断行为。若修改共享契约中的默认亮度，云端 API 与编排器也需重启；开发 API 的文件监视范围是 `cloud/`，不会因只修改 `edge/` 而自动重载。

### 3. 打开手机 PWA

浏览器访问已配置的同源 HTTPS 地址（现有部署为 `https://ssm.eliottxu.top`）：点击“创建空间”进入 `main` 管理会话，添加发现的设备后分享邀请二维码；其他人扫码作为空间参与者加入体验。HTTPS 部署保持 `SSM_COOKIE_SECURE=true`，仅本机 HTTP 调试设为 `false`。

公开部署前必须拆分 `ssm_api`、`ssm_orchestrator`、`esp32_desk` 三个 MQTT 账号并启用 ACL。用 `mosquitto_passwd` 配置账号，复制 `infra/broker/acl.example` 为 `infra/broker/acl`，启用 `mosquitto.conf.tmpl` 中的 `acl_file`；云端分别填写 `MQTT_API_*` / `MQTT_ORCHESTRATOR_*`，ESP32 使用设备账号。ACL 使只有 API/编排器能发布 task，避免在资源受限的 MicroPython 固件中分发云端 HMAC 密钥。旧版本曾将 broker 凭据随 PWA 发布过，部署时应轮换旧凭据；ACL 尚未启用前，不要把 broker 暴露给不可信网络。

## 网络拓扑

```
手机浏览器（HTTPS）
    │
    ▼
Cloudflare Tunnel（https://ssm.eliottxu.top）  ← 提供 HTTPS
    │
    ▼ HTTP（端口 8080）
nginx
    ├── /        → PWA 静态文件（端口 8081）
    └── /api     → FastAPI（端口 8082，REST + SSE）

ESP32
    └── TCP 1883 → Mosquitto（当前模板未启用 TLS，按部署网络限制访问）
```

Mosquitto 只服务 ESP32、FastAPI 与编排器等内部节点；当前模板只开启 TCP 1883，没有 WebSocket 9001 监听。PWA 不连接 broker。上述 nginx / tunnel 路由属于部署配置，仓库 Makefile 不负责安装或生成它们。

## 数据流示例

**传感器自动响应（灯已显式进入 `reactive`，读数新鲜且不在让位窗口）：**

```
光线变暗
  → BSM 检测 GPIO34 ADC 值下降 → event_cb("LIGHT_CHANGED", {level: "DARK"})
  → TriggerMap → ISM 状态变更 → 发布 ssm/agents/esp32_desk_light/event
  → 云端灯 Graph 结合传感器事实、教导和空间上下文判断是否调灯
  → 若决定执行，发布 ssm/task/esp32_desk_led/{task_id}（具体颜色/亮度由模型选择）
  → ESP32 执行 → 发布 ssm/result/... {ok}
```

**手机自然语言控制：**

```
用户说"把灯调暗一点"
  → 手机 POST /api/intents（安全 Cookie + CSRF）
  → FastAPI 生成 actor/persist/session_id 可信上下文并发布内部 ssm/intent/{session_id}
  → 管家 LangGraph 按序派发模型返回的工具调用（ESP32 走 MQTT，灯语义任务 / Go2 走内部 HTTP）
  → 每步等待真实结果并保存检查点，成功后继续；失败重新规划，未确认则暂停后续调用
  → 最终回复 → FastAPI 按 session_id 过滤反馈 → 手机 SSE 收到结果
```

## 开发与验证

优先运行改动相关的现有检查。Python 使用 pytest；`*_checks.py` 不在默认文件发现规则内，需显式传入，或使用下列发现规则包含它们：

```bash
# 示例：状态图、能力链与当前教导上下文契约
uv run python -m pytest cloud/cards/region_contract_checks.py cloud/esp32/capability_chain_checks.py cloud/agents/common/training/context_checks.py -q

# 需要全量回归时，同时发现 test_*.py、*_test.py 和 *_checks.py
uv run python -m pytest cloud -o 'python_files=test_*.py *_test.py *_checks.py' -q

git diff --check
```

前端回归脚本在 `scripts/check_*.cjs`；按脚本头部配置本地 Node 依赖、Chrome 与 React UMD 路径后执行，例如 `node scripts/check_activity_frontend.cjs`。这些工具不属于 PWA 运行或构建步骤。模拟回归、真实模型质量和真机验证需分别记录；验证使用临时数据目录，不覆盖现有教导、人员、人格或任务账本。

开发约束见 [AGENTS.md](AGENTS.md)，日志与追踪见 [OBSERVABILITY.md](OBSERVABILITY.md)。历史迁移计划保留当时的验收记录，旧教学数据不自动迁移或启用。
