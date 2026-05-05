# SSM 项目 — Claude 工作指引

## MVP 原则

**只构建能验证想法的最简实现，不做推测性功能。**

- 每个任务：改一个文件、加一个行为、5 分钟内可验证
- 不需要证明概念的功能一律不做
- 不做辅助抽象、不做面向未来的设计、不做多余的错误处理
- 有疑问时：更少的代码，不是更多

## 项目概述

Smart System Mesh —— 以 MQTT 为消息总线的多智能体 IoT 系统。

- **消息代理**：Mosquitto，运行在云服务器（`47.116.137.202`）
  - TCP `1883` —— ESP32 连接
  - WebSocket `9001` —— PWA 连接（经 nginx 代理）
  - 启动：`mosquitto -c /root/ssm/broker/mosquitto.conf -d`
- **边缘智能体**：ESP32，运行 MicroPython（`agents/esp32/`）
- **控制界面**：手机 PWA（`agents/phone/`）
- **云端智能体**：运行在 `/root/ssm/.venv`，uv 管理，Python 3.13
- **硬件引脚**：WS2812 灯环数据线（GPIO4）、蜂鸣器（GPIO5）、光线传感器（GPIO18 数字）、红外（GPIO19）、声音（GPIO15）；GPIO16/GPIO17 空闲

## 目录结构

```
ssm/
├── agents/
│   ├── esp32/          ← MicroPython 边缘智能体
│   └── phone/          ← PWA 控制界面（React）
├── broker/             ← Mosquitto 配置
├── server/
│   ├── .env            ← 所有云服务共用的环境变量
│   ├── api/
│   │   └── main.py     ← FastAPI：/api/chat、/api/nlu
│   └── orchestrator/
│       ├── main.py     ← PC 智能体入口（MQTT → LangGraph）
│       ├── graph.py    ← LangGraph 决策图（V1 ReAct + V2 编排器）
│       ├── tools.py    ← LLM 工具函数
│       └── shared_state.py ← 跨节点共享状态
├── docs/
│   └── ARCHITECTURE.md ← 通信架构单一真实来源
└── Makefile            ← 统一服务管理
```

## 服务启动

所有服务通过 `Makefile` 统一管理，在 `/root/ssm` 目录下执行：

```bash
make broker        # 启动 MQTT Broker（后台守护进程）
make api           # 启动 Chat API（前台，端口 8082）
make api-bg        # 启动 Chat API（后台，日志 → /tmp/ssm_api.log）
make orchestrator  # 启动 PC 决策智能体（前台）
make pwa           # 启动 PWA 静态服务（前台，端口 8081）
make pwa-bg        # 启动 PWA 静态服务（后台）
make ngrok         # 启动 ngrok 隧道（前台）
make ngrok-bg      # 启动 ngrok（后台）
make ps            # 查看 SSM 相关进程
make logs          # 查看后台日志
make ngrok-url     # 查询当前 ngrok 公网地址
```

其他基础设施：
```bash
systemctl start nginx   # nginx 反向代理（开机自启，配置：/etc/nginx/conf.d/ssm.conf）
```

## 网络架构

```
手机浏览器（HTTPS）
    │
    ▼
ngrok 公网域名（https://xxx.ngrok-free.dev）  ← 提供 HTTPS/WSS，重启后地址变
    │
    ▼ HTTP（端口 8080）
nginx
    ├── /        → uv python http.server（端口 8081）  ← PWA 静态文件
    └── /mqtt    → mosquitto WebSocket（端口 9001）    ← MQTT over WSS

ESP32
    └── TCP 1883 → mosquitto                           ← 直连，无需 TLS
```

PWA 内 MQTT 地址自动切换：
- HTTPS 访问时 → `wss://{ngrok域名}/mqtt`
- HTTP 访问时 → `ws://47.116.137.202:9001`（直连，局域网调试用）

## Python 环境（uv）

项目使用 uv 管理（`pyproject.toml` + `uv.lock`，位于 `/root/ssm`）。

```bash
# 添加新依赖
uv add <包名>

# 从锁文件同步安装所有依赖
uv sync

# 运行脚本（uv 自动使用 .venv，无需手动激活）
uv run python <脚本>
```

- 始终在 `/root/ssm`（项目根目录）执行 uv 命令
- 使用 `uv run python` 而非手动激活虚拟环境

## 架构规则

### ISM（接口状态机）
- 每个 agent unit 一个 ISM 实例（光线传感器、红外、LED、蜂鸣器各一个）
- ISM 只知道状态和触发器——不涉及硬件，不涉及 MQTT
- 转换表是合法操作的唯一真实来源

### BSM（行为状态机）
- 整个 ESP32 只有一个 BSM——它是硬件驱动层
- BSM 通过回调触发事件，不了解 MQTT
- BSM 不直接调用 ISM

### TriggerMap
- MQTT ↔ ISM ↔ BSM 接线的唯一位置
- 使 ISM 和 BSM 彼此完全解耦

## 标准 4 类消息协议（每个 agent unit）

每个智能体（传感器或执行器）精确发布以下 4 类 topic：

| 序号 | 类型 | Topic 模式 | 发布方 | 保留 |
|------|------|-----------|--------|------|
| 1 | `manifest` | `ssm/agents/{id}/manifest` | 启动时 | 是 |
| 2 | `state` | `ssm/agents/{id}/state` | 状态变化时 | 是 |
| 3 | `event` | `ssm/agents/{id}/event` | 事件发生时 | 否 |
| 4 | `report` | `ssm/agents/{id}/report` | 传感器：观测值；执行器：执行反馈 | 否 |
| 5 | `location` | `ssm/agents/{id}/location` | 启动时 | 是 |

位置消息格式：`{"agent": "{id}", "lng": float, "lat": float, "type": "fixed", "ts": int}`
- 当前 ESP32 使用固定坐标（`config.py` 中 `LOCATION_LNG/LAT`），GCJ-02 坐标系
- PWA 订阅此 topic，结合手机 GPS 计算距离并排序设备列表

## 编码规范

- ESP32 只使用 MicroPython——不用 CPython 专属库
- 所有 MQTT payload 均为 JSON（简单标志可用纯字符串）
- 每条发布消息都带 `ts` 字段（Unix 时间戳）
- 每条发布消息都带 `agent_id` 字段
