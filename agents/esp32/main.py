# main.py — SSM ESP32 Agent entry point (runs after boot.py)
# Architecture: BSM (hardware) ← TriggerMap → ISM (contract) ↔ MQTT

import time
from config import AGENT_ID, LOCAL_RULES_DELAY_MS, HEARTBEAT_MS
from config import AGENT_LIGHT, AGENT_IR, AGENT_LED, AGENT_BUZ
from config import LOCATION_LNG, LOCATION_LAT
from ism         import ISM, State, SENSOR_TABLE, LED_TABLE, BUZZER_TABLE
from bsm         import BSM
from mqtt_client import MqttClient
from trigger_map import TriggerMap
from local_rules import LocalRules
import agent_manifest

print("[Main] SSM {} starting...".format(AGENT_ID))

# ── Instantiate components ────────────────────────────────────
mqtt = MqttClient()

ism_light = ISM(State.SAMPLING,   AGENT_LIGHT, SENSOR_TABLE)
ism_ir    = ISM(State.MONITORING, AGENT_IR,    SENSOR_TABLE)
ism_led   = ISM(State.OFF,        AGENT_LED,   LED_TABLE)
ism_buz   = ISM(State.SILENT,     AGENT_BUZ,   BUZZER_TABLE)

local_rules = LocalRules(mqtt)

bsm = BSM(event_cb=lambda e, d: None)  # placeholder

trigger_map = TriggerMap(
    bsm         = bsm,
    ism_led     = ism_led,
    ism_light   = ism_light,
    ism_ir      = ism_ir,
    ism_buz     = ism_buz,
    mqtt        = mqtt,
    local_rules = local_rules,
)

bsm._event_cb = trigger_map.on_bsm_event

# ── 重连后重新发布函数 ────────────────────────────────────────
def on_reconnect():
    print("[Main] Reconnected — re-publishing manifests and states")
    agent_manifest.publish(mqtt)
    mqtt.publish("ssm/agents/{}/location".format(AGENT_ID),
                 {"agent": AGENT_ID, "lng": LOCATION_LNG, "lat": LOCATION_LAT,
                  "type": "fixed", "ts": time.time()}, retain=True)
    mqtt.publish("ssm/agents/{}/state".format(AGENT_LED),
                 {"agent": AGENT_LED, "ism": ism_led.state, "ts": time.time()}, retain=True)
    mqtt.publish("ssm/agents/{}/state".format(AGENT_BUZ),
                 {"agent": AGENT_BUZ, "ism": ism_buz.state, "ts": time.time()}, retain=True)

# ── MQTT setup ───────────────────────────────────────────────
mqtt.set_callback(trigger_map.on_mqtt)
mqtt.set_reconnect_callback(on_reconnect)

mqtt.subscribe("ssm/agents/{}/command".format(AGENT_LED))
mqtt.subscribe("ssm/agents/{}/command".format(AGENT_BUZ))
mqtt.subscribe("ssm/task/{}/+".format(AGENT_LED))
mqtt.subscribe("ssm/task/{}/+".format(AGENT_BUZ))
mqtt.subscribe("ssm/decision/active")
mqtt.subscribe("ssm/sys/ping")

mqtt.begin()

# ── 首次发布 manifest + 初始状态 ─────────────────────────────
agent_manifest.publish(mqtt)

mqtt.publish("ssm/agents/{}/location".format(AGENT_ID),
             {"agent": AGENT_ID, "lng": LOCATION_LNG, "lat": LOCATION_LAT,
              "type": "fixed", "ts": time.time()}, retain=True)

mqtt.publish("ssm/agents/{}/state".format(AGENT_LED),
             {"agent": AGENT_LED, "ism": ism_led.state, "ts": time.time()}, retain=True)
mqtt.publish("ssm/agents/{}/state".format(AGENT_BUZ),
             {"agent": AGENT_BUZ, "ism": ism_buz.state, "ts": time.time()}, retain=True)

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
    if time.ticks_diff(now, _last_heartbeat) >= 5000:   # 临时改成5秒打印一次确认循环在跑
        _last_heartbeat = now
        print("[Loop] alive — light={} ir={}".format(bsm.light_level, bsm.ir_presence))
        mqtt.publish("ssm/agents/{}/heartbeat".format(AGENT_ID), {
            "ts":    time.time(),
            "light": bsm.light_level,
            "ir":    bsm.ir_presence,
        })

    time.sleep_ms(10)
