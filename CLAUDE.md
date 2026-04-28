# SSM Project — Claude Instructions

## MVP Principle

**Build the simplest thing that demonstrates the idea. No speculative features.**

- Each task: one file changed, one behavior added, verifiable in < 5 minutes
- If a feature isn't needed to prove the concept, leave it out
- No helper abstractions, no future-proofing, no extra error handling
- When in doubt: fewer lines, not more

## Project Overview

Smart System Mesh — a multi-agent IoT system using MQTT as the message bus.

- **Broker**: Mosquitto on Linux cloud server (`47.116.137.202`)
  - TCP `1883` — ESP32 连接
  - WebSocket `9001` — PWA 连接（经 nginx 代理）
  - 启动：`mosquitto -c /root/ssm/broker/mosquitto.conf -d`
- **Edge agent**: ESP32 with MicroPython (`agents/esp32/`)
- **Control UI**: PWA on phone (`agents/phone/`)
- **PC Agent**: runs in `/root/ssm/.venv` (uv-managed, Python 3.13)
- **Hardware**: RGB LED (GPIO 4/16/17), buzzer (GPIO5), light sensor (GPIO18 digital), IR (GPIO19), sound (GPIO15)

## 服务启动

云服务器上需要同时运行以下进程：

```bash
# 1. MQTT Broker（已持久运行）
mosquitto -c /root/ssm/broker/mosquitto.conf -d

# 2. PWA 静态文件服务（端口 8081）
nohup bash -c "cd /root/ssm/agents/phone && uv run python -m http.server 8081" > /tmp/pwa.log 2>&1 &

# 3. nginx 反向代理（开机自启，systemd 管理）
systemctl start nginx   # 配置：/etc/nginx/conf.d/ssm.conf

# 4. ngrok HTTPS 隧道（需手动启动，重启后地址会变）
nohup ngrok http 8080 --log=stdout > /tmp/ngrok.log 2>&1 &

# 查询当前 ngrok 地址
curl -s http://localhost:4040/api/tunnels | python3 -c \
  "import sys,json;[print(t['public_url']) for t in json.load(sys.stdin)['tunnels']]"
```

## 网络架构

```
手机浏览器 (HTTPS)
    │
    ▼
ngrok 公网域名 (https://xxx.ngrok-free.dev)   ← 提供 HTTPS/WSS，地址重启后变
    │
    ▼ HTTP (port 8080)
nginx
    ├── /        → uv python http.server (port 8081)   ← PWA 静态文件
    └── /mqtt    → mosquitto WebSocket (port 9001)      ← MQTT over WSS

ESP32
    └── TCP 1883 → mosquitto                            ← 直连，无需 TLS
```

PWA 里 MQTT 地址自动切换：
- HTTPS 访问时 → `wss://{ngrok域名}/mqtt`
- HTTP 访问时  → `ws://47.116.137.202:9001`（直连，局域网调试用）

## Python Environment (uv)

Project is managed with uv (`pyproject.toml` + `uv.lock` at `/root/ssm`).

```bash
# Add a new dependency
uv add <package>

# Sync / install all dependencies from lockfile
uv sync

# Run scripts — uv automatically uses .venv, no activation needed
uv run python <script>
```

- Always run uv commands from `/root/ssm` (project root)
- Use `uv run python` instead of activating the venv manually

## Architecture Rules

### ISM (Interface State Machine)
- One ISM instance **per agent unit** (light sensor, IR sensor, LED, buzzer)
- ISM only knows states and triggers — no hardware, no MQTT
- Transition table is the single source of truth for what's allowed

### BSM (Behavior State Machine)
- One BSM for the whole ESP32 — it's the hardware driver layer
- BSM fires events via callback; it does not know about MQTT
- BSM never calls ISM directly

### TriggerMap
- The only place where MQTT ↔ ISM ↔ BSM wiring happens
- Keeps ISM and BSM completely decoupled from each other

## Standard 4-Message Protocol (per agent unit)

Every agent (sensor or actuator) publishes exactly these 4 topic types:

| # | Type | Topic pattern | Published by | Retained |
|---|------|--------------|--------------|---------|
| 1 | `manifest` | `ssm/agents/{id}/manifest` | on boot | yes |
| 2 | `state` | `ssm/agents/{id}/state` | on change | yes |
| 3 | `event` | `ssm/agents/{id}/event` | on occurrence | no |
| 4 | `report` | `ssm/agents/{id}/report` | sensing agents: observation; actuator agents: execution feedback | no |
| 5 | `location` | `ssm/agents/{id}/location` | on boot | yes |

location payload: `{"agent": "{id}", "lng": float, "lat": float, "type": "fixed", "ts": int}`
- 当前 ESP32 用固定坐标（`config.py` 中 `LOCATION_LNG/LAT`），GCJ-02 坐标系
- PWA 订阅此 topic，结合手机 GPS 计算距离并排序设备列表

## Coding Conventions

- MicroPython only on ESP32 — no CPython-only libs
- All MQTT payloads are JSON (or plain string for simple flags)
- `ts` field (unix timestamp) on every published message
- `agent_id` field on every published message
