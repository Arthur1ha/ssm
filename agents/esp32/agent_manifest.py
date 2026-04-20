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
        "levels": ["DARK", "DIM", "NORMAL", "BRIGHT"],
        "ism_states": ["SAMPLING", "ERROR"],
    }, ts)

    _pub(mqtt, AGENT_IR, "sensor", "ir_presence", {
        "values": [True, False],
        "ism_states": ["MONITORING", "ERROR"],
    }, ts)

    _pub(mqtt, AGENT_SOUND, "sensor", "sound", {
        "values": ["detected"],
    }, ts)

    _pub(mqtt, AGENT_LED, "actuator", "rgb_led", {
        "commands": ["SET_COLOR", "SET_STATE", "BLINK"],
        "ism_states": ["OFF", "DIM", "BRIGHT", "COLOR", "BLINK"],
    }, ts)

    _pub(mqtt, AGENT_BUZ, "actuator", "buzzer", {
        "commands": ["PLAY", "STOP"],
        "ism_states": ["SILENT", "ALERT", "NOTIFY"],
    }, ts)

    print("[Manifest] Published 5 unit manifests for {}".format(AGENT_ID))
