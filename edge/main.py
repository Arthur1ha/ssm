# main.py — SSM ESP32 Agent entry point (runs after boot.py)
# Architecture: BSM (hardware) ← TriggerMap → ISM (contract) ↔ MQTT

import time
from config import DEVICE_ID, LOCAL_RULES_DELAY_MS, HEARTBEAT_MS
from config import UNIT_LIGHT, UNIT_IR, UNIT_SOUND, UNIT_LED
from config import LOCATION_LNG, LOCATION_LAT
from ism         import ISM, State, SENSOR_TABLE, LED_TABLE
from bsm         import BSM
from mqtt_client import MqttClient
from trigger_map import TriggerMap
from local_rules import LocalRules
from probe       import PRESENCE
import agent_manifest

print("[Main] SSM {} starting...".format(DEVICE_ID))

# ── Instantiate components ────────────────────────────────────
mqtt = MqttClient()

ism_light = ISM(State.SAMPLING,   UNIT_LIGHT, SENSOR_TABLE)
ism_ir    = ISM(State.MONITORING, UNIT_IR,    SENSOR_TABLE)
ism_led   = ISM(State.OFF,        UNIT_LED,   LED_TABLE)

local_rules = LocalRules()

bsm = BSM(event_cb=lambda e, d: None)  # placeholder

trigger_map = TriggerMap(
    bsm         = bsm,
    ism_led     = ism_led,
    ism_light   = ism_light,
    ism_ir      = ism_ir,
    mqtt        = mqtt,
    local_rules = local_rules,
)

bsm._event_cb = trigger_map.on_bsm_event
# 本地兜底规则命中时直接走 TriggerMap 执行口（不经 MQTT 自环）
local_rules.set_led_executor(trigger_map.exec_command)

# ── 重连后重新发布函数 ────────────────────────────────────────
def on_reconnect():
    print("[Main] Reconnected — re-publishing manifests and states")
    local_rules._load_from_file()
    agent_manifest.publish(mqtt)
    mqtt.publish("ssm/agents/{}/location".format(DEVICE_ID),
                 {"unit_id": DEVICE_ID, "lng": LOCATION_LNG, "lat": LOCATION_LAT,
                  "type": "fixed", "ts": time.time()}, retain=True)
    mqtt.publish("ssm/agents/{}/state".format(UNIT_LED),
                 {"unit_id": UNIT_LED, "ism": ism_led.state, "ts": time.time()}, retain=True)
    if PRESENCE.get(UNIT_SOUND):
        mqtt.publish("ssm/agents/{}/state".format(UNIT_SOUND),
                     {"unit_id": UNIT_SOUND, "detected": False, "ts": time.time()}, retain=True)

# ── MQTT setup ───────────────────────────────────────────────
mqtt.set_callback(trigger_map.on_mqtt)
mqtt.set_reconnect_callback(on_reconnect)

mqtt.subscribe("ssm/task/{}/+".format(UNIT_LED))
mqtt.subscribe("ssm/decision/active")
mqtt.subscribe("ssm/rules/{}".format(DEVICE_ID))
mqtt.subscribe("ssm/agents/{}/led_mood".format(UNIT_LED))

mqtt.begin()

# ── 首次发布 manifest + 初始状态 ─────────────────────────────
agent_manifest.publish(mqtt)

mqtt.publish("ssm/agents/{}/location".format(DEVICE_ID),
             {"unit_id": DEVICE_ID, "lng": LOCATION_LNG, "lat": LOCATION_LAT,
              "type": "fixed", "ts": time.time()}, retain=True)

mqtt.publish("ssm/agents/{}/state".format(UNIT_LED),
             {"unit_id": UNIT_LED, "ism": ism_led.state, "ts": time.time()}, retain=True)

if PRESENCE.get(UNIT_SOUND):
    mqtt.publish("ssm/agents/{}/state".format(UNIT_SOUND),
                 {"unit_id": UNIT_SOUND, "detected": False, "ts": time.time()}, retain=True)

# ── 等待 retained decision/active 到达再激活本地规则 ──────────
print("[Main] Waiting {}ms before local rules activate...".format(LOCAL_RULES_DELAY_MS))
time.sleep_ms(LOCAL_RULES_DELAY_MS)
print("[Main] Local rules armed. Entering main loop.")

# ── 主循环 ───────────────────────────────────────────────────
_last_heartbeat = time.ticks_ms()

while True:
    mqtt.check_msg()
    bsm.tick()

    now = time.ticks_ms()
    if time.ticks_diff(now, _last_heartbeat) >= HEARTBEAT_MS:
        _last_heartbeat = now
        status = "light={}".format(bsm.light_level)
        if PRESENCE.get(UNIT_IR):
            status += " ir={}".format(bsm.ir_presence)
        print("[Loop] alive —", status)
        if PRESENCE.get(UNIT_SOUND):
            mqtt.publish("ssm/agents/{}/state".format(UNIT_SOUND),
                         {"unit_id": UNIT_SOUND, "detected": False, "ts": time.time()}, retain=True)

    time.sleep_ms(10)
