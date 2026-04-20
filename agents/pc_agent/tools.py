# tools.py — LangChain tools injected into Decision / Evaluation agents.
# Call init() once before building the graph.

import json
from langchain_core.tools import tool

_state  = None
_mqtt   = None


def init(shared_state, mqtt_client):
    global _state, _mqtt
    _state = shared_state
    _mqtt  = mqtt_client


# ── Decision agent tools ──────────────────────────────────────

@tool
def get_sensor_snapshot() -> str:
    """
    Get current readings from all connected sensor agents (light, IR, sound).
    Returns JSON with unit_id → { state, report } data.
    """
    return json.dumps(_state.sensor_snapshot(), ensure_ascii=False, indent=2)


@tool
def publish_led_command(cmd: str, state: str = "OFF",
                        r: int = 255, g: int = 255, b: int = 255,
                        brightness: int = 200, count: int = 3) -> str:
    """
    Send a command to the RGB LED actuator.

    cmd="SET_STATE"  → state="OFF" | "BRIGHT" | "DIM"
    cmd="SET_COLOR"  → r, g, b (0-255), brightness (0-255)
    cmd="BLINK"      → r, g, b, count (number of blinks)
    """
    topic = _state.get_command_topic("rgb_led")
    if not topic:
        return "ERROR: RGB LED unit not yet discovered (no manifest received)"

    payload: dict = {"cmd": cmd}
    if cmd == "SET_STATE":
        payload["state"] = state
    elif cmd in ("SET_COLOR", "BLINK"):
        payload.update({"r": r, "g": g, "b": b, "brightness": brightness})
        if cmd == "BLINK":
            payload["count"] = count

    _mqtt.publish(topic, json.dumps(payload))
    _state.set_last_decision({"tool": "publish_led_command", "payload": payload})
    return f"Published {json.dumps(payload)} → {topic}"


@tool
def publish_buzzer_command(cmd: str, pattern: str = "NOTIFY") -> str:
    """
    Send a command to the buzzer actuator.
    cmd="PLAY"  → pattern="NOTIFY" | "ALERT"
    cmd="STOP"  → stop current sound
    """
    topic = _state.get_command_topic("buzzer")
    if not topic:
        return "ERROR: Buzzer unit not yet discovered"

    payload: dict = {"cmd": cmd}
    if cmd == "PLAY":
        payload["pattern"] = pattern

    _mqtt.publish(topic, json.dumps(payload))
    return f"Published {json.dumps(payload)} → {topic}"


# ── Evaluation agent tools ────────────────────────────────────

@tool
def get_last_decision() -> str:
    """
    Get the last command issued by the Decision Agent.
    Use this to compare what was intended vs what the actuator reported.
    """
    d = _state.last_decision()
    return json.dumps(d, ensure_ascii=False) if d else "No decision recorded yet"


@tool
def get_actuator_snapshot() -> str:
    """
    Get the latest state and report from all actuator agents (LED, buzzer).
    The 'report' field contains execution feedback: cmd, result (ok/blocked), ism_state.
    """
    return json.dumps(_state.actuator_snapshot(), ensure_ascii=False, indent=2)


@tool
def publish_assessment(result: str, reason: str) -> str:
    """
    Publish an evaluation assessment to ssm/decision/evaluation.
    result: "ok" | "mismatch" | "blocked" | "retry_needed"
    reason: human-readable explanation of the evaluation outcome
    """
    payload = json.dumps({"result": result, "reason": reason})
    _mqtt.publish("ssm/decision/evaluation", payload)
    return f"Assessment published: {result} — {reason}"
