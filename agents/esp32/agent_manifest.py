# agent_manifest.py — Publish one retained manifest per agent unit on startup.
# Topics: ssm/agents/{unit_id}/manifest
import time
from config import AGENT_ID, FIRMWARE_VER, AGENT_LIGHT, AGENT_IR, AGENT_SOUND, AGENT_LED, AGENT_BUZ


def _pub(mqtt, unit_id, agent_type, name, extra, ts):
    base = "ssm/agents/{}".format(unit_id)
    manifest = {
        "unit_id":      unit_id,
        "parent_id":    AGENT_ID,
        "agent_type":   agent_type,   # "sensor" or "actuator"
        "name":         name,
        "hw_platform":  "esp32",
        "firmware_ver": FIRMWARE_VER,
        "ts": ts,
        "topics": {
            "manifest": base + "/manifest",
            "state":    base + "/state",
            "event":    base + "/event",
            "report":   base + "/report",
        }
    }
    if agent_type == "actuator":
        manifest["topics"]["command"] = base + "/command"
    manifest.update(extra)
    mqtt.publish(base + "/manifest", manifest, retain=True)


def publish(mqtt):
    ts = time.time()

    _pub(mqtt, AGENT_LIGHT, "sensor", "ambient_light", {
        "agent_tag": "light_level",
        "levels": ["DARK", "DIM", "NORMAL", "BRIGHT"],
        "ism_states": ["SAMPLING", "ERROR"],
    }, ts)

    _pub(mqtt, AGENT_IR, "sensor", "ir_presence", {
        "agent_tag": "presence",
        "values": [True, False],
        "ism_states": ["MONITORING", "ERROR"],
    }, ts)

    _pub(mqtt, AGENT_SOUND, "sensor", "sound", {
        "agent_tag": "sound",
        "values": ["detected"],
    }, ts)

    _pub(mqtt, AGENT_LED, "actuator", "ws2812_ring", {
        "num_pixels": 16,
        "commands": ["SET_COLOR", "SET_STATE", "BLINK"],
        "ism_states": ["OFF", "DIM", "BRIGHT", "COLOR", "BLINK"],
        "capabilities": [
            {"action": "SET_COLOR", "params": ["r", "g", "b", "brightness"]},
            {"action": "SET_STATE", "params": ["state"], "values": ["ON", "OFF", "BRIGHT", "DIM"]},
            {"action": "BLINK",     "params": ["r", "g", "b", "count"]},
        ],
        "resource_tags": ["lighting", "ambiance"],
    }, ts)

    _pub(mqtt, AGENT_BUZ, "actuator", "buzzer", {
        "commands": ["PLAY", "STOP"],
        "ism_states": ["SILENT", "ALERT", "NOTIFY"],
        "capabilities": [
            {"action": "PLAY", "params": ["pattern"], "values": ["NOTIFY", "ALERT"]},
            {"action": "STOP", "params": []},
        ],
        "resource_tags": ["alert", "notification"],
    }, ts)

    print("[Manifest] Published 5 unit manifests for {}".format(AGENT_ID))
