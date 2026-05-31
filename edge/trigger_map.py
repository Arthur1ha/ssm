# trigger_map.py — ISM ↔ BSM bridge
# All topics follow: ssm/agents/{unit_id}/{state|event|report|command}

import time
from ism import Trigger
from config import AGENT_ID, AGENT_LIGHT, AGENT_IR, AGENT_SOUND, AGENT_LED

_LED_STATE_TRIG = {
    "OFF":    Trigger.CMD_OFF,
    "BRIGHT": Trigger.CMD_BRIGHT,
    "DIM":    Trigger.CMD_DIM,
}


class TriggerMap:
    def __init__(self, bsm, ism_led, ism_light, ism_ir, mqtt, local_rules):
        self._bsm        = bsm
        self._ism_led    = ism_led
        self._ism_light  = ism_light
        self._ism_ir     = ism_ir
        self._mqtt       = mqtt
        self._local      = local_rules

        self._led_cmd_topic  = "ssm/agents/{}/command".format(AGENT_LED)
        self._led_task_pfx   = "ssm/task/{}/".format(AGENT_LED)
        self._rules_topic    = "ssm/rules/{}".format(AGENT_ID)
        self._led_mood_topic = "ssm/agents/desk/led_mood"

    # ─────────────────────────────────────────────────────────
    #  Called by MqttClient for every incoming message
    # ─────────────────────────────────────────────────────────
    def on_mqtt(self, topic, payload):
        if topic == self._led_cmd_topic:
            if isinstance(payload, dict):
                cmd = payload.get("cmd")
                ok = self._exec_led(cmd, payload)
                self._publish_led_report(cmd, ok)

        elif topic.startswith(self._led_task_pfx):
            task_id = topic[len(self._led_task_pfx):]
            self._handle_led_task(payload, task_id)

        elif topic == "ssm/decision/active":
            active = (payload == "true" or payload is True)
            self._local.set_decision_active(active)

        elif topic == self._rules_topic:
            if isinstance(payload, list):
                self._local.load_rules(payload)

        elif topic == self._led_mood_topic:
            if isinstance(payload, dict):
                mood = payload.get("mood", "idle")
                self._bsm.led_mood_set(mood)

        elif topic == "ssm/sys/ping":
            self._mqtt.publish("ssm/sys/pong/{}".format(AGENT_ID),
                               {"ts": time.time()})

    # ─────────────────────────────────────────────────────────
    #  LED execution — ISM validates first, then BSM acts
    # ─────────────────────────────────────────────────────────
    def _exec_led(self, action, params):
        if action == "SET_COLOR":
            r   = int(params.get("r", 255))
            g   = int(params.get("g", 255))
            b   = int(params.get("b", 255))
            bri = int(params.get("brightness", 200))
            ok = self._ism_led.transition(Trigger.CMD_COLOR)
            if ok:
                self._bsm.led_set_color(r, g, b, bri)

        elif action == "SET_STATE":
            st   = params.get("state")
            trig = _LED_STATE_TRIG.get(st) if st else None
            if not trig:
                return False
            ok = self._ism_led.transition(trig)
            if ok:
                self._bsm.led_set_state(st)

        elif action == "BLINK":
            r   = int(params.get("r", 255))
            g   = int(params.get("g", 255))
            b   = int(params.get("b", 255))
            cnt = int(params.get("count", 3))
            ok = self._ism_led.transition(Trigger.CMD_BLINK)
            if ok:
                self._bsm.led_blink(r, g, b, cnt)

        else:
            return False

        self._publish_led_state()
        return ok

    # ─────────────────────────────────────────────────────────
    #  Called by BSM event_cb for hardware-originated events
    # ─────────────────────────────────────────────────────────
    def on_bsm_event(self, event, data):
        ts = time.time()

        if event in ("LIGHT_CHANGED", "LIGHT_HEARTBEAT"):
            payload = {"agent": AGENT_LIGHT, "value": data["value"],
                       "level": data["level"], "ts": ts}
            self._mqtt.publish("ssm/agents/{}/state".format(AGENT_LIGHT),
                               payload, retain=True)
            if event == "LIGHT_CHANGED":
                self._mqtt.publish("ssm/agents/{}/event".format(AGENT_LIGHT), payload)
                self._local.on_light_event(data["level"])
            self._mqtt.publish("ssm/agents/{}/report".format(AGENT_LIGHT), {
                "agent": AGENT_LIGHT, "level": data["level"],
                "type": "observation", "ts": ts
            })
            if self._ism_light.state == "ERROR":
                self._ism_light.transition(Trigger.SENSOR_RECOVERED)

        elif event in ("IR_CHANGED", "IR_HEARTBEAT"):
            payload = {"agent": AGENT_IR, "presence": data["presence"],
                       "raw": data["raw"], "ts": ts}
            self._mqtt.publish("ssm/agents/{}/state".format(AGENT_IR),
                               payload, retain=True)
            if event == "IR_CHANGED":
                self._mqtt.publish("ssm/agents/{}/event".format(AGENT_IR), payload)
                self._local.on_ir_event(data["presence"])
            self._mqtt.publish("ssm/agents/{}/report".format(AGENT_IR), {
                "agent": AGENT_IR, "presence": data["presence"],
                "type": "observation", "ts": ts
            })

        elif event == "SOUND_DETECTED":
            payload = {"agent": AGENT_SOUND, "detected": True, "ts": ts}
            self._mqtt.publish("ssm/agents/{}/event".format(AGENT_SOUND), payload)
            self._mqtt.publish("ssm/agents/{}/report".format(AGENT_SOUND), {
                "agent": AGENT_SOUND, "type": "observation", "detected": True, "ts": ts
            })
            self._local.on_sound_event()

        elif event == "BLINK_DONE":
            self._ism_led.transition(Trigger.BLINK_DONE)
            self._publish_led_state()

    # ─────────────────────────────────────────────────────────
    #  Task handler (V2 Orchestrator protocol)
    #  Topic: ssm/task/{device_id}/{task_id}
    # ─────────────────────────────────────────────────────────
    def _handle_led_task(self, p, task_id):
        session_id = p.get("session_id", "") if isinstance(p, dict) else ""
        action     = p.get("action") if isinstance(p, dict) else None
        params     = p.get("params", {}) if isinstance(p, dict) else {}
        ok = self._exec_led(action, params) if action else False
        self._mqtt.publish("ssm/result/{}/{}".format(AGENT_LED, task_id), {
            "task_id": task_id, "session_id": session_id,
            "result": "ok" if ok else "blocked",
            "ism_state": self._ism_led.state, "ts": time.time(),
        })

    def _publish_led_state(self):
        self._mqtt.publish("ssm/agents/{}/state".format(AGENT_LED), {
            "agent": AGENT_LED, "ism": self._ism_led.state, "ts": time.time()
        }, retain=True)

    def _publish_led_report(self, cmd, ok):
        self._mqtt.publish("ssm/agents/{}/report".format(AGENT_LED), {
            "agent": AGENT_LED, "cmd": cmd,
            "result": "ok" if ok else "blocked",
            "ism_state": self._ism_led.state, "ts": time.time()
        })
