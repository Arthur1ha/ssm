# SSM Project — Claude Instructions

## MVP Principle

**Build the simplest thing that demonstrates the idea. No speculative features.**

- Each task: one file changed, one behavior added, verifiable in < 5 minutes
- If a feature isn't needed to prove the concept, leave it out
- No helper abstractions, no future-proofing, no extra error handling
- When in doubt: fewer lines, not more

## Project Overview

Smart System Mesh — a multi-agent IoT system using MQTT as the message bus.

- **Broker**: Mosquitto on Windows (`tools/broker_start.bat`)
- **Edge agent**: ESP32 with MicroPython (`agents/esp32/`)
- **Control UI**: PWA on phone, served locally (`tools/pwa_server.bat`)
- **Hardware**: RGB LED (GPIO 4/16/17), buzzer (GPIO5), light sensor (GPIO18 digital), IR (GPIO19), sound (GPIO15)

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

## Coding Conventions

- MicroPython only on ESP32 — no CPython-only libs
- All MQTT payloads are JSON (or plain string for simple flags)
- `ts` field (unix timestamp) on every published message
- `agent_id` field on every published message
