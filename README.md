# SSM — 智能系统网格（Smart System Mesh）

无中心的分布式多智能体 IoT 系统：以 MQTT 为统一**控制总线**（发现、在线状态、能力声明 card），**数据面按设备本性选传输**（ESP32 走 MQTT、Go2 走 HTTP/SSE）。连接 ESP32 边缘设备、云端 AI 智能体（编排器 / ESP32 桌面智能体 / Go2 机器狗）和手机 PWA 控制界面。新智能体通过 A2A Agent Card 暴露能力即可被动态发现与调用。

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    MQTT 内部控制总线                         │
│                       Mosquitto :1883                        │
└──────────┬────────────────────────────┬─────────────────────┘
           │                            │
  ┌────────▼─────────┐        ┌─────────▼──────────────────────┐
  │  ESP32（边缘端）  │        │       云端服务（cloud/）         │
  │  MicroPython     │        │  ┌──────────────────────────┐  │
  │                  │        │  │ orchestrator 编排器       │  │
  │  BSM ← TriggerMap│        │  │ Planner→Dispatcher→       │  │
  │  ISM ← TriggerMap│        │  │ Evaluator→Responder       │  │
  │  本地规则（兜底） │        │  ├──────────────────────────┤  │
  └────────┬─────────┘        │  │ api（FastAPI :8082）      │  │
           │           HTTP   │  │  esp32 桌面智能体          │  │
           │        ◄────────►│  │  go2 机器狗智能体（WebRTC）│  │
           │                  │  └──────────────────────────┘  │
  ┌────────▼──────────────────────────────────────────────────┤
  │              手机 PWA（发现 + 监控 + 对话）                  │
  │          HTTPS REST + 会话隔离 SSE，不持有 MQTT 凭据         │
  └─────────────────────────────────────────────────────────────┘
```

**控制优先级**：用户指令 > 智能体 > 本地规则（离线兜底）

云端有三个 LLM 智能体：
- **编排器**（`cloud/orchestrator/`，独立进程）：订阅用户意图，经 LangGraph 图统筹全局，MQTT 派发到 ESP32、HTTP 委托到 Go2。
- **ESP32 桌面智能体**（`cloud/esp32/agent.py`）：带 persona 的 sense→reason→act，自主响应传感器事件。
- **Go2 机器狗智能体**（`cloud/go2/`）：独立 LangGraph，含性格演化、记忆、导航、视觉，经 HTTP 暴露 `/api/go2/*`。

## esp硬件引脚

| 硬件 | GPIO | 模式 |
|------|------|------|
| WS2812 灯环（数据线） | GPIO4 | 单总线 |
| 光线传感器 | GPIO34 | 模拟（ADC1） |
| 声音传感器 | GPIO15 | 数字，上升沿触发 |

传感器是否在线由 `edge/probe.py` 在启动时自动探测（拉电阻技巧），无需手动配置开关。具体接线以 `edge/config.py` 的 `UNIT_CONFIGS` 为准。

## 各组件说明

### ESP32 边缘端（`edge/`）

| 文件 | 职责 |
|------|------|
| `boot.py` | 启动时连接 WiFi |
| `config.py` | 引脚、MQTT 地址、时序常量、**UNIT_CONFIGS**（unit 注册表） |
| `local_config.py` | 本机 WiFi/MQTT 密钥（从 example 复制，git 忽略） |
| `probe.py` | 启动时自动探测各引脚是否有传感器接线 |
| `bsm.py` | 行为状态机——GPIO/PWM/ADC 驱动，事件回调 |
| `ism.py` | 接口状态机——仅处理状态转换，不涉及硬件 |
| `trigger_map.py` | BSM ↔ ISM ↔ MQTT 接线（唯一耦合点） |
| `agent_manifest.py` | 启动时为已探测到的 unit 发布 retained manifest |
| `local_rules.py` | 云端智能体离线时的本地自动规则 |
| `mqtt_client.py` | MQTT 封装，支持自动重连和 LWT |
| `main.py` | 主循环入口 |

**本地规则**（`ssm/decision/active = "false"` 时启用）：
- 光线 DARK/DIM → 暖白光 LED（R=255, G=160, B=60）
- 光线 BRIGHT → 关灯
- 检测到声音 → 白色闪烁 ×2（始终生效）

### 云端服务（`cloud/`）

环境变量统一存放在 `cloud/.env`（见 `cloud/.env.example`）。编排器 / Go2 / ESP32 智能体均用火山方舟 Ark `deepseek-v4-*`（`MODEL_LIST`）。

Go2 人脸身份使用腾讯云 IAI 3.0 人员库。部署时在 `cloud/.env` 配置
`TENCENTCLOUD_SECRET_ID`、`TENCENTCLOUD_SECRET_KEY`、`TENCENTCLOUD_REGION` 与
`TENCENT_FACE_GROUP_ID`；人员库必须已存在且 `FaceModelVersion=3.0`。识别由
`GO2_FACE_RECOGNITION_ENABLED` 控制，登记/补脸/删除另受默认关闭的
`GO2_FACE_ENROLLMENT_ENABLED` 保护。登记完成后应重新关闭该写开关。

本地只保存 `PersonId`、姓名、关系和人脸数量到
`cloud/go2/data/memory/people.json`，不会保存原始照片或 embedding。可用
`RUN_TENCENT_FACE_LIVE=1 uv run pytest cloud/go2/tests/test_tencent_face_live.py -q`
检查人员库版本；该测试默认跳过，也不会创建或删除人员。

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
| `GET/POST/DELETE /api/rules` | 管理员自动化规则 CRUD（`rules.py`） |
| `/api/admin/*` | 固定空间和访客邀请管理 |
| `/api/go2/*` | 内部 Go2 数据面；写接口要求内部服务身份与可信执行上下文 |

`api/main.py` 同时承载一个 ESP32 MQTT 桥，订阅 `manifest/state/event/result` 并喂给 ESP32 桌面智能体。

#### 编排器（`cloud/orchestrator/`，独立进程）

Python + LangGraph，订阅 `ssm/intent/+`，驱动编排图。

| 文件 | 职责 |
|------|------|
| `main.py` | MQTT 事件循环：意图 → 编排图 |
| `graph.py` | LangGraph：Planner→Dispatcher→Evaluator→Responder（含多模型 fallback） |
| `tools.py` | MQTT/HTTP 派发与反馈辅助 |
| `shared_state.py` | 线程安全设备/任务快照（合并 MQTT manifest + `devices.json`） |
| `devices.json` | 预留：文件型非 MQTT 设备注册表（当前为空 `{}`，go2 已走 MQTT card） |
| `rules.json` | 自动化规则存储 |

> 设备能力卡注册表见 `cloud/cards/`（`registry.py` 订阅 card/manifest/status，是 A2A 单一真相）。

### 手机 PWA（`app/`）

无构建步骤的 React 应用（Babel standalone）。浏览器只连接 FastAPI：REST 负责登录、命令与管理，认证 SSE 负责实时设备状态和编排反馈；浏览器不加载 MQTT.js，也不持有 broker 写凭据。

系统只有一个固定空间 `main`。用户点击“创建空间”即可进入并管理规则、邀请与长期资产；体验者扫描邀请二维码获得短期 Cookie 会话，只能使用已有能力。界面形态为**单屏聊天中心 + hash 设备子页**，访客界面不会显示长期配置入口。

## MQTT 消息协议

**完整定义以 `protocol/topics.md` 为单一真实来源**，下表为概览：

| 类型 | Topic | 保留 | 发布者 | 说明 |
|------|-------|------|--------|------|
| `manifest` | `ssm/agents/{unit_id}/manifest` | 是 | ESP32 | 设备能力声明，启动时发布；空 payload = 单元缺席移除 |
| `card` | `ssm/agents/{unit_id}/card` | 是 | Go2 | 完整 AgentCard JSON（自描述设备） |
| `state` | `ssm/agents/{unit_id}/state` | 是 | ESP32 | 当前 ISM 状态，变更时发布 |
| `event` | `ssm/agents/{unit_id}/event` | 否 | ESP32 | 传感器事件 |
| `status` | `ssm/agents/{device_id}/status` | 是 | ESP32 / Go2 | `online`/`offline`（掉线由 LWT 自动置 offline）|
| `location` | `ssm/agents/{device_id}/location` | 是 | ESP32 | 设备地理坐标（GCJ-02） |

**控制 / 编排 / 表达 topic**：
- `ssm/intent/{session_id}` —— FastAPI 验证会话后发布的内部意图（HMAC + 时间窗 + nonce，编排器拒绝篡改与重放）
- `ssm/task/{unit_id}/{task_id}` —— 任务下发（云端 → 设备）
- `ssm/result/{unit_id}/{task_id}` —— 任务结果（设备 → 云端，`ok`/`blocked`）
- `ssm/feedback/{session_id}` —— 编排器渐进式执行反馈
- `ssm/decision/active` —— 云端在线总开关（`true`/`false`，离线时 ESP32 本地规则接管）
- `ssm/rules/{device_id}` —— 自动化规则（retained）
- `ssm/agents/{unit_id}/thought` —— 拟人化思考

所有 payload 均为 JSON，每条消息带 `unit_id`（自标识键）和 `ts`（Unix 时间戳）。

## 快速上手

### 0. Python 环境

```bash
uv sync          # 安装所有依赖
uv add <包名>    # 添加新依赖
```

始终在项目根目录执行 uv 命令；运行脚本用 `uv run python`，无需激活虚拟环境。

### 1. 启动云端服务

```bash
make broker        # 启动 MQTT Broker（前台，tail 日志）
make api           # 前台启动 FastAPI（端口 8082，--reload）
make orchestrator  # 前台启动编排器（崩溃自动重启）
make pwa-bg        # 后台启动 PWA 文件服务（端口 8081）
make tunnel-bg     # 后台启动 Cloudflare Tunnel HTTPS 隧道
make tunnel-url    # 输出公网地址（https://ssm.eliottxu.top）
make ps / logs     # 查看进程 / 日志
```

> 服务建议在 tmux 窗口中前台运行，便于观察日志；带 `-bg` 的目标为后台幂等启动。`make api` 的 `--reload` 只在代码变更时重载，Ctrl+C 会停止前台 API。API 窗口直接显示日志，并同时写入 `logs/api.log`。

### 按会话查看日志

使用 `make trace-task ID=<session_id>` 查看一个会话内灯、机器狗和编排器的步骤；加 `JSON=1` 可看脱敏后的完整字段，加 `FOLLOW=1` 可持续跟踪。也可用 `trace_id` 定位某一次任务。默认输出包含模型输入输出、决策、工具参数、接收方与结果摘要。`make logs` 显示最近会话及错误摘要。在现有 tmux 会话内运行 `make tmux-windows` 会按名称增补 `session_trace`、`errors`、`mqtt-trace` 窗口；`make tmux-windows ID=<session_id>` 将任务窗口切到指定会话。也可从会话外设置 `SSM_TMUX_SESSION=<会话名>`；设置 `SERIAL_PORT=<设备端口>` 时增补 ESP32 串口窗口。重复执行不会重启 `api`、`orchestrator` 或 `broker` 服务窗口。

任务事件在首次发生时追加到 `logs/session-<session_id>.jsonl`，API 与编排器共享同一个会话文件；没有会话 ID 的自主任务写入 `logs/session-system.jsonl`。每条事件写完立即关闭文件，不会因进程启动产生空文件，文件删除后下一个事件会重新创建。启动时清理 14 天未更新的会话日志。运行状态 JSON 位于各智能体的 `data/runs/`，不随日志清理删除。`make rotate-logs` 对 API、编排器等 stdout 和 Broker 日志执行独立轮转；真实设备验收后可将其纳入每日定时任务。API 普通日志写入 `api.log`（包含云端灯和机器狗），编排器普通日志写入 `orchestrator.log`；Broker/串口原始日志各自保留。

旧 SQLite 任务账本迁移时，先停止 API/编排器写入并备份数据库及 `-wal`、`-shm` 文件，再执行 `uv run python -m scripts.migrate_task_sqlite_to_json --source <旧库> --dest <数据目录>/runs --kind runs --apply`；灯语义账本使用 `--kind semantic_tasks` 和 `semantic_tasks` 目标目录。省略 `--apply` 可先盘点。脚本不会删除旧库，目标 JSON 内容不同则停止。运行服务现使用 `SSM_GO2_RUN_DIR`、`SSM_ORCHESTRATOR_RUN_DIR` 覆盖目录；旧 `*_RUN_DB` 仅供定位迁移源。

### 2. 配置并上传 ESP32

复制 `edge/local_config.example.py` 为 git 忽略的 `edge/local_config.py`，填入 WiFi 与 Broker 凭据，再通过 [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html) 上传：

```bash
cd edge
mpremote connect <端口> cp boot.py clock.py config.py local_config.py probe.py ism.py bsm.py \
  mqtt_client.py trigger_map.py local_rules.py agent_manifest.py main.py :
mpremote connect <端口> reset
```

上传后串口应输出 `[Clock] Unix time synced: ...` 和 `[Probe] {...}`。若 NTP 暂不可用，设备继续运行并每分钟重试；校时成功后会重新发布 retained 状态。

### 3. 打开手机 PWA

部署时配置 MQTT 凭据，并在 API 与编排器共用的 `cloud/.env` 中填写一次 `SSM_INTERNAL_SERVICE_TOKEN`；无需单独设置 `SSM_MQTT_SIGNING_SECRET`。浏览器访问 `https://ssm.eliottxu.top`：点击“创建空间”成为 `main` 空间管理者，添加设备后分享二维码；其他人扫码作为空间参与者加入体验。未配置 `SSM_SESSION_SECRET` 时，服务使用进程内临时密钥，重启服务后现有会话会失效。

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
    └── TCP 1883 → Mosquitto（直连，无需 TLS）
```

Mosquitto 只服务 ESP32、FastAPI 与编排器等内部节点；PWA 不连接 broker。

## 数据流示例

**传感器自动响应：**
```
光线变暗
  → BSM 检测 GPIO34 ADC 值下降 → event_cb("LIGHT_CHANGED", {level: "DARK"})
  → TriggerMap → ISM 状态变更 → 发布 ssm/agents/esp32_desk_light/event
  → 云端 ESP32 桌面智能体收到事件 → 推理
  → 发布 ssm/task/esp32_desk_led/{task_id}（SET_COLOR 暖白）
  → ESP32 执行 → 发布 ssm/result/... {ok}
```

**手机自然语言控制：**
```
用户说"把灯调暗一点"
  → 手机 POST /api/intents（安全 Cookie + CSRF）
  → FastAPI 生成 actor/persist/session_id 可信上下文并发布内部 ssm/intent/{session_id}
  → 编排器 Planner 生成任务 → Dispatcher 下发（ESP32 走 MQTT / Go2 走 HTTP）
  → 设备执行 → Evaluator 确认 → FastAPI 按 session_id 过滤反馈 → 手机 SSE 收到结果
```
