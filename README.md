# SSM — Smart System Mesh

A multi-agent IoT system that connects an ESP32, a PC AI decision agent, and a phone PWA over MQTT.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        MQTT Broker                          │
│                    Mosquitto :1883/:9001                     │
└──────────────────────┬──────────────────┬───────────────────┘
                       │                  │
          ┌────────────▼───────┐   ┌──────▼────────────┐
          │   ESP32 (Edge)     │   │  PC Decision Agent │
          │  MicroPython       │   │  LangGraph + LLM   │
          │                    │   │                    │
          │  BSM ← TriggerMap  │   │  Decision Agent    │
          │  ISM ← TriggerMap  │   │  Evaluation Agent  │
          │  LocalRules        │   │                    │
          └────────┬───────────┘   └──────────────────┬─┘
                   │                                   │
          ┌────────▼───────────────────────────────────▼─────┐
          │               Phone PWA (Supervision)            │
          │         WebSocket MQTT, manual override          │
          └───────────────────────────────────────────────────┘
```

**Control priority**: Phone override > PC Decision Agent > ESP32 local rules (autonomous fallback)

## Hardware

| Component | GPIO | Mode |
|---|---|---|
| RGB LED — Red | GPIO4 | PWM |
| RGB LED — Green | GPIO16 | PWM |
| RGB LED — Blue | GPIO17 | PWM |
| Buzzer (passive) | GPIO5 | PWM |
| Light sensor | GPIO18 | Digital (binary) |
| IR sensor | GPIO19 | Digital, active LOW |
| Sound sensor | GPIO15 | Digital, rising edge |

## Components

### ESP32 (`agents/esp32/`)

MicroPython code running on the edge device.

| File | Role |
|---|---|
| `boot.py` | WiFi connection on startup |
| `config.py` | Pins, MQTT broker, timing constants |
| `bsm.py` | Hardware driver — GPIO, PWM, ADC, event callbacks |
| `ism.py` | Interface State Machine — state transitions only, no hardware |
| `trigger_map.py` | Wires BSM ↔ ISM ↔ MQTT (the only coupling point) |
| `local_rules.py` | Autonomous rules when PC agent is offline |
| `agent_manifest.py` | Publishes retained manifests on boot |
| `mqtt_client.py` | MQTT wrapper with auto-reconnect and LWT |
| `main.py` | Orchestrator — runs the main loop |

**ISM units**: `esp32_desk_light`, `esp32_desk_ir`, `esp32_desk_sound`, `esp32_desk_led`, `esp32_desk_buz`

**Local rules** (active when `ssm/decision/active = "false"`):
- Light DARK/DIM → warm white LED (R=255, G=160, B=60)
- Light BRIGHT → LED off
- Sound detected → blink white ×2 (always active)

### PC Decision Agent (`agents/pc_agent/`)

Python 3.11 + LangGraph. Subscribes to all sensor events, issues LED/buzzer commands, and evaluates execution.

| File | Role |
|---|---|
| `main.py` | MQTT ↔ LangGraph bridge, event queue |
| `graph.py` | LangGraph graph with Decision and Evaluation agents |
| `shared_state.py` | Thread-safe state store (sensors, actuators, decisions) |
| `tools.py` | LangChain tools: `publish_led_command`, `publish_buzzer_command`, `get_sensor_snapshot` |
| `.env` | API key, model, MQTT broker URL |

**Decision rules** (encoded in system prompt):
- Light DARK/DIM → `SET_COLOR` warm white
- Light BRIGHT → `SET_STATE OFF`
- Sound detected → `BLINK` white ×2

**Evaluation agent**: Compares last decision vs. actuator report → `ok | blocked | mismatch | retry_needed`

### Phone PWA (`agents/phone/`)

Progressive Web App served over HTTP, connects to broker via WebSocket (port 9001).

Features: live agent state panel, AI decision log, event log, manual LED/buzzer control.

No build step — plain HTML/CSS/JS.

## MQTT Protocol

Every agent unit publishes exactly 4 topic types:

| Type | Topic | Retained | Description |
|---|---|---|---|
| `manifest` | `ssm/agents/{id}/manifest` | yes | Capabilities, states, commands — published on boot |
| `state` | `ssm/agents/{id}/state` | yes | Current ISM state — published on change |
| `event` | `ssm/agents/{id}/event` | no | Sensor occurrence or actuator trigger |
| `report` | `ssm/agents/{id}/report` | no | Sensor observation or actuator execution feedback |

Control topics:
- `ssm/decision/active` — `"true"` = PC/phone in control, suppresses local rules
- `ssm/decision/evaluation` — Evaluation Agent result
- `ssm/agents/{id}/command` — LED/buzzer commands

All payloads are JSON with `agent_id` and `ts` (unix timestamp) fields.

## Setup

### 0. Python Environment

Managed with [uv](https://github.com/astral-sh/uv) via `pyproject.toml` + `uv.lock`.

```bash
# Install all dependencies
uv sync

# Add a new dependency
uv add <package>
```

No need to activate — use `uv run python` to run any script and uv will automatically pick up `.venv`.

### 1. MQTT Broker

Requires [Mosquitto](https://mosquitto.org/download/) installed (`yum install -y mosquitto`).

```bash
mosquitto -c /root/ssm/broker/mosquitto.conf -d
```

Listens on TCP 1883 (ESP32 + PC Agent) and WebSocket 9001 (Phone PWA).  
Authentication is required — credentials are in `broker/passwd`.

### 2. ESP32

Edit `agents/esp32/config.py` — set WiFi credentials, broker IP, and MQTT auth.

Upload via [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html):

```bash
cd agents/esp32
mpremote connect COM7 cp boot.py config.py ism.py bsm.py mqtt_client.py trigger_map.py local_rules.py agent_manifest.py main.py :
mpremote connect COM7 reset
```

Or use [Thonny](https://thonny.org/) — open each file and save to device.

### 3. PC Decision Agent

Edit `agents/pc_agent/.env` — set `OPENAI_API_KEY`.

```bash
cd /root/ssm/agents/pc_agent
uv run python main.py
```

### 4. Phone PWA

```bash
cd /root/ssm/agents/phone
uv run python -m http.server 8080
```

Open `http://47.116.137.202:8080` on your phone.

## Data Flow Example

```
Light sensor darkens
  → BSM detects GPIO18 LOW → fires event_cb("LIGHT_CHANGED", {level: "DARK"})
  → TriggerMap transitions ISM_LIGHT, publishes ssm/agents/esp32_desk_light/event
  → PC Agent receives event → enqueues → Decision Agent runs
  → Decision Agent calls publish_led_command(SET_COLOR, r=255, g=160, b=60)
  → ESP32 receives ssm/agents/esp32_desk_led/command
  → TriggerMap transitions ISM_LED → BSM sets RGB PWM
  → TriggerMap publishes ssm/agents/esp32_desk_led/report {result: "ok"}
  → Evaluation Agent compares decision vs. report → publishes "ok"
```

## Design Principles

- **ISM** knows only states and valid transitions — no hardware, no MQTT
- **BSM** drives hardware and fires events upward — no MQTT, no ISM
- **TriggerMap** is the only file that connects all three layers
- Every added behavior is one file change, verifiable in under 5 minutes
