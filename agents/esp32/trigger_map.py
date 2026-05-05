# trigger_map.py — ISM ↔ BSM bridge
# All topics follow: ssm/agents/{unit_id}/{state|event|report|command}

import time
from ism import Trigger
from config import AGENT_LIGHT, AGENT_IR, AGENT_SOUND, AGENT_LED, AGENT_BUZ


class TriggerMap:
    def __init__(self, bsm, ism_led, ism_light, ism_ir, ism_buz, mqtt, local_rules):
        self._bsm        = bsm
        self._ism_led    = ism_led
        self._ism_light  = ism_light
        self._ism_ir     = ism_ir
        self._ism_buz    = ism_buz
        self._mqtt       = mqtt
        self._local      = local_rules

        self._led_cmd_topic  = "ssm/agents/{}/command".format(AGENT_LED)
        self._buz_cmd_topic  = "ssm/agents/{}/command".format(AGENT_BUZ)
        self._led_task_pfx   = "ssm/task/{}/".format(AGENT_LED)
        self._buz_task_pfx   = "ssm/task/{}/".format(AGENT_BUZ)

    # ─────────────────────────────────────────────────────────
    #  Called by MqttClient for every incoming message
    # ─────────────────────────────────────────────────────────
    def on_mqtt(self, topic, payload):
        if topic == self._led_cmd_topic:
            self._handle_led(payload)

        elif topic == self._buz_cmd_topic:
            self._handle_buzzer(payload)

        elif topic.startswith(self._led_task_pfx):
            task_id = topic[len(self._led_task_pfx):]
            self._handle_led_task(payload, task_id)

        elif topic.startswith(self._buz_task_pfx):
            task_id = topic[len(self._buz_task_pfx):]
            self._handle_buz_task(payload, task_id)

        elif topic == "ssm/decision/active":
            active = (payload == "true" or payload is True)
            self._local.set_decision_active(active)

        elif topic == "ssm/sys/ping":
            self._mqtt.publish("ssm/sys/pong/{}".format(AGENT_LED),
                               {"ts": time.time()})

    def _handle_led(self, p):
        cmd = p.get("cmd") if isinstance(p, dict) else None

        if cmd == "SET_COLOR":
            r   = int(p.get("r", 255))
            g   = int(p.get("g", 255))
            b   = int(p.get("b", 255))
            bri = int(p.get("brightness", 255))
            self._bsm.led_set_color(r, g, b, bri)
            ok = self._ism_led.transition(Trigger.CMD_COLOR)
            self._publish_led_state()
            self._publish_led_report(cmd, ok)

        elif cmd == "SET_STATE":
            state = p.get("state", "OFF")
            self._bsm.led_set_state(state)
            trig = {"OFF": Trigger.CMD_OFF, "BRIGHT": Trigger.CMD_BRIGHT,
                    "DIM":  Trigger.CMD_DIM}.get(state, Trigger.CMD_OFF)
            ok = self._ism_led.transition(trig)
            self._publish_led_state()
            self._publish_led_report(cmd, ok)

        elif cmd == "BLINK":
            r   = int(p.get("r", 255))
            g   = int(p.get("g", 255))
            b   = int(p.get("b", 255))
            cnt = int(p.get("count", 3))
            self._bsm.led_blink(r, g, b, cnt)
            ok = self._ism_led.transition(Trigger.CMD_BLINK)
            self._publish_led_state()
            self._publish_led_report(cmd, ok)

    def _handle_buzzer(self, p):
        cmd = p.get("cmd") if isinstance(p, dict) else None
        if cmd == "PLAY":
            pattern = p.get("pattern", "NOTIFY")
            self._bsm.buzzer_play(pattern)
            trig = Trigger.PLAY_NOTIFY if pattern == "NOTIFY" else Trigger.PLAY_ALERT
            ok = self._ism_buz.transition(trig)
            self._publish_buz_state()
            self._publish_buz_report(cmd, ok)
        elif cmd == "STOP":
            self._bsm.buzzer_stop()
            ok = self._ism_buz.transition(Trigger.STOP_SOUND)
            self._publish_buz_state()
            self._publish_buz_report(cmd, ok)

    # ─────────────────────────────────────────────────────────
    #  Called by BSM event_cb for hardware-originated events
    # ─────────────────────────────────────────────────────────
    def on_bsm_event(self, event, data):
        ts = time.time()

        if event in ("LIGHT_CHANGED", "LIGHT_HEARTBEAT"):
            payload = {"agent": AGENT_LIGHT, "value": data["value"],
                       "level": data["level"], "ts": ts}
            # state: retained current reading
            self._mqtt.publish("ssm/agents/{}/state".format(AGENT_LIGHT),
                               payload, retain=True)
            # event: non-retained change notification
            if event == "LIGHT_CHANGED":
                self._mqtt.publish("ssm/agents/{}/event".format(AGENT_LIGHT), payload)
                self._local.on_light_event(data["level"])
            # report: observation for decision layer
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

        elif event == "SOUND_DONE":
            self._ism_buz.transition(Trigger.SOUND_DONE)
            self._publish_buz_state()

    # ─────────────────────────────────────────────────────────
    #  Publish helpers
    # ─────────────────────────────────────────────────────────
    # ─────────────────────────────────────────────────────────
    #  Task handlers (V2 Orchestrator protocol)
    #  Topic: ssm/task/{device_id}/{task_id}
    #  Payload: {task_id, session_id, action, params, ts}
    # ─────────────────────────────────────────────────────────
    def _handle_led_task(self, p, task_id):
        session_id = p.get("session_id", "") if isinstance(p, dict) else ""
        action     = p.get("action") if isinstance(p, dict) else None
        params     = p.get("params", {}) if isinstance(p, dict) else {}

        ok = False
        if action == "SET_COLOR":
            self._bsm.led_set_color(
                int(params.get("r", 255)), int(params.get("g", 255)),
                int(params.get("b", 255)), int(params.get("brightness", 200)))
            ok = self._ism_led.transition(Trigger.CMD_COLOR)
            self._publish_led_state()
        elif action == "SET_STATE":
            st = params.get("state", "OFF")
            self._bsm.led_set_state(st)
            trig = {"OFF": Trigger.CMD_OFF, "BRIGHT": Trigger.CMD_BRIGHT,
                    "DIM": Trigger.CMD_DIM}.get(st, Trigger.CMD_OFF)
            ok = self._ism_led.transition(trig)
            self._publish_led_state()
        elif action == "BLINK":
            self._bsm.led_blink(
                int(params.get("r", 255)), int(params.get("g", 255)),
                int(params.get("b", 255)), int(params.get("count", 3)))
            ok = self._ism_led.transition(Trigger.CMD_BLINK)
            self._publish_led_state()

        self._mqtt.publish("ssm/result/{}/{}".format(AGENT_LED, task_id), {
            "task_id": task_id, "session_id": session_id,
            "result": "ok" if ok else "blocked",
            "ism_state": self._ism_led.state, "ts": time.time(),
        })

    def _handle_buz_task(self, p, task_id):
        session_id = p.get("session_id", "") if isinstance(p, dict) else ""
        action     = p.get("action") if isinstance(p, dict) else None
        params     = p.get("params", {}) if isinstance(p, dict) else {}

        ok = False
        if action == "PLAY":
            pattern = params.get("pattern", "NOTIFY")
            self._bsm.buzzer_play(pattern)
            trig = Trigger.PLAY_NOTIFY if pattern == "NOTIFY" else Trigger.PLAY_ALERT
            ok = self._ism_buz.transition(trig)
            self._publish_buz_state()
        elif action == "STOP":
            self._bsm.buzzer_stop()
            ok = self._ism_buz.transition(Trigger.STOP_SOUND)
            self._publish_buz_state()

        self._mqtt.publish("ssm/result/{}/{}".format(AGENT_BUZ, task_id), {
            "task_id": task_id, "session_id": session_id,
            "result": "ok" if ok else "blocked",
            "ism_state": self._ism_buz.state, "ts": time.time(),
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

    def _publish_buz_state(self):
        self._mqtt.publish("ssm/agents/{}/state".format(AGENT_BUZ), {
            "agent": AGENT_BUZ, "ism": self._ism_buz.state, "ts": time.time()
        }, retain=True)

    def _publish_buz_report(self, cmd, ok):
        self._mqtt.publish("ssm/agents/{}/report".format(AGENT_BUZ), {
            "agent": AGENT_BUZ, "cmd": cmd,
            "result": "ok" if ok else "blocked",
            "ism_state": self._ism_buz.state, "ts": time.time()
        })
