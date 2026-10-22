# trigger_map.py — ISM ↔ BSM bridge
# All topics follow: ssm/agents/{unit_id}/{state|event}

import time
from ism import State, Trigger
from config import DEVICE_ID, UNIT_LIGHT, UNIT_IR, UNIT_SOUND, UNIT_LED

_LED_STATE_TRIG = {
    "OFF":    Trigger.CMD_OFF,
    "ON":     Trigger.CMD_ON,
    "BRIGHT": Trigger.CMD_COLOR,  # migration aliases
    "DIM":    Trigger.CMD_COLOR,
}


class TriggerMap:
    def __init__(self, bsm, ism_led, ism_light, ism_ir, mqtt, local_rules):
        self._bsm        = bsm
        self._ism_led    = ism_led
        self._ism_light  = ism_light
        self._ism_ir     = ism_ir
        self._mqtt       = mqtt
        self._local      = local_rules
        self._pending_blink = None
        self._last_on = (255, 255, 255, 200)
        self._blink_restore = None
        self._blink_id = None
        self._blink_config = None
        self._last_led_snapshot = None
        self._recent_results = []  # bounded QoS-1 duplicate suppression

        self._led_task_pfx = "ssm/task/{}/".format(UNIT_LED)
        self._rules_topic  = "ssm/rules/{}".format(DEVICE_ID)

    # ─────────────────────────────────────────────────────────
    #  Called by MqttClient for every incoming message
    # ─────────────────────────────────────────────────────────
    def on_mqtt(self, topic, payload):
        if topic.startswith(self._led_task_pfx):
            task_id = topic[len(self._led_task_pfx):]
            self._handle_led_task(payload, task_id)

        elif topic == "ssm/decision/active":
            active = (payload == "true" or payload is True)
            self._local.set_decision_active(active)

        elif topic == self._rules_topic:
            if isinstance(payload, list):
                self._local.load_rules(payload)

    # ─────────────────────────────────────────────────────────
    #  本地兜底规则直接调用（不经 MQTT 自环）：执行 LED 命令
    # ─────────────────────────────────────────────────────────
    def exec_command(self, payload):
        """供 LocalRules 在云端离线时直接调用，执行一条 LED 命令。"""
        if isinstance(payload, dict):
            try:
                return self._exec_led(payload.get("cmd"), payload)
            except Exception as exc:
                print("[LocalRules] command failed:", exc)
                return False

    # ─────────────────────────────────────────────────────────
    #  LED execution — ISM validates first, then BSM acts
    # ─────────────────────────────────────────────────────────
    def _exec_led(self, action, params):
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        for key in ("r", "g", "b", "brightness"):
            if key in params and (type(params[key]) is not int or not 0 <= params[key] <= 255):
                raise ValueError("{} must be an integer in 0..255".format(key))
        if action == "BLINK" and (type(params.get("count", 3)) is not int or not 1 <= params.get("count", 3) <= 20):
            raise ValueError("count must be an integer in 1..20")
        trigger = {"SET_COLOR": Trigger.CMD_COLOR, "BLINK": Trigger.CMD_BLINK}.get(action)
        if action == "SET_STATE":
            trigger = _LED_STATE_TRIG.get(params.get("state"))
        if action == "SET_COLOR":
            color = (params.get("r", 255), params.get("g", 255),
                     params.get("b", 255), params.get("brightness", 200))
            if color[3] == 0 or not any(color[:3]):
                trigger = Trigger.CMD_OFF
        elif action == "SET_STATE" and params.get("state") in ("BRIGHT", "DIM"):
            brightness = 255 if params["state"] == "BRIGHT" else 40
            color = (255, 255, 255, brightness)
        if not trigger or not self._ism_led.allows(trigger):
            return False
        # Hardware runs before committing the public state; failures never announce a target state.
        if action == "SET_COLOR" or (action == "SET_STATE" and params.get("state") in ("BRIGHT", "DIM")):
            if trigger == Trigger.CMD_OFF:
                self._bsm.led_set_state("OFF")
            else:
                self._bsm.led_set_color(*color)
                self._last_on = color
        elif action == "SET_STATE":
            if trigger == Trigger.CMD_OFF:
                self._bsm.led_set_state("OFF")
            else:
                self._bsm.led_set_color(*self._last_on)
        elif action == "BLINK":
            blink = (params.get("r", 255), params.get("g", 255),
                     params.get("b", 255), params.get("count", 3))
            blink_id = self._bsm.led_blink(*blink)

        else:
            return False

        was_blinking = self._ism_led.state == State.BLINKING
        if action == "BLINK":
            if not was_blinking:
                self._blink_restore = self._ism_led.state
            self._blink_id = blink_id
            self._blink_config = blink
        elif was_blinking:
            self._blink_restore = None
            self._blink_id = None
            self._blink_config = None
        ok = self._ism_led.transition(trigger)
        if self._pending_blink:
            tid, sid = self._pending_blink
            self._pending_blink = None
            self._publish_result(tid, sid, "cancelled", "superseded")
        self._publish_led_state()
        return ok

    # ─────────────────────────────────────────────────────────
    #  Called by BSM event_cb for hardware-originated events
    # ─────────────────────────────────────────────────────────
    def on_bsm_event(self, event, data):
        ts = time.time()

        if event in ("LIGHT_CHANGED", "LIGHT_HEARTBEAT"):
            payload = {"unit_id": UNIT_LIGHT, "value": data["value"],
                       "level": data["level"], "ts": ts}
            self._mqtt.publish("ssm/agents/{}/state".format(UNIT_LIGHT),
                               payload, retain=True)
            if event == "LIGHT_CHANGED":
                self._mqtt.publish("ssm/agents/{}/event".format(UNIT_LIGHT), payload)
                self._local.on_light_event(data["level"])
            if self._ism_light.state == "ERROR":
                self._ism_light.transition(Trigger.SENSOR_RECOVERED)

        elif event in ("IR_CHANGED", "IR_HEARTBEAT"):
            payload = {"unit_id": UNIT_IR, "presence": data["presence"],
                       "raw": data["raw"], "ts": ts}
            self._mqtt.publish("ssm/agents/{}/state".format(UNIT_IR),
                               payload, retain=True)
            if event == "IR_CHANGED":
                self._mqtt.publish("ssm/agents/{}/event".format(UNIT_IR), payload)
                self._local.on_ir_event(data["presence"])

        elif event == "SOUND_DETECTED":
            payload = {"unit_id": UNIT_SOUND, "detected": True, "ts": ts}
            self._mqtt.publish("ssm/agents/{}/event".format(UNIT_SOUND), payload)
            self._local.on_sound_event()

        elif event == "BLINK_DONE":
            if (self._ism_led.state != State.BLINKING or
                    data.get("blink_id") != self._blink_id):
                return
            target = self._blink_restore
            # Restore pixels before announcing the stable state or task success.
            try:
                if target == State.ON:
                    self._bsm.led_set_color(*self._last_on)
                else:
                    self._bsm.led_set_state("OFF")
            except Exception as exc:
                if self._pending_blink:
                    tid, sid = self._pending_blink
                    self._pending_blink = None
                    self._publish_result(tid, sid, "error", str(exc))
                return
            self._blink_restore = None
            self._blink_id = None
            self._blink_config = None
            self._ism_led.transition(Trigger.BLINK_DONE, target)
            self._publish_led_state()
            if self._pending_blink:
                tid, sid = self._pending_blink
                self._pending_blink = None
                self._publish_result(tid, sid, "ok")

    # ─────────────────────────────────────────────────────────
    #  Orchestrator task handler
    #  Topic: ssm/task/{unit_id}/{task_id}
    # ─────────────────────────────────────────────────────────
    def _handle_led_task(self, p, task_id):
        for result in self._recent_results:
            if result["task_id"] == task_id:
                self._mqtt.publish("ssm/result/{}/{}".format(UNIT_LED, task_id), result)
                return
        if self._pending_blink and self._pending_blink[0] == task_id:
            self._publish_result(task_id, self._pending_blink[1], "executing")
            return
        session_id = p.get("session_id", "") if isinstance(p, dict) else ""
        action     = p.get("action") if isinstance(p, dict) else None
        params     = p.get("params", {}) if isinstance(p, dict) else {}
        try:
            ok = self._exec_led(action, params) if action else False
        except (ValueError, TypeError) as exc:
            self._publish_result(task_id, session_id, "blocked", str(exc))
            return
        except Exception as exc:
            self._publish_result(task_id, session_id, "error", str(exc))
            return
        if ok and action == "BLINK":
            self._pending_blink = (task_id, session_id)
            self._publish_result(task_id, session_id, "executing")
        else:
            self._publish_result(task_id, session_id, "ok" if ok else "blocked",
                                 "" if ok else "invalid_action_or_state")

    def _publish_result(self, task_id, session_id, result, error=""):
        payload = {
            "unit_id": UNIT_LED,
            "task_id": task_id, "session_id": session_id,
            "result": result, "error": error,
            "ism_state": self._ism_led.state, "ts": time.time(),
        }
        if result != "executing":
            self._recent_results.append(payload)
            if len(self._recent_results) > 16:
                self._recent_results.pop(0)
        self._mqtt.publish("ssm/result/{}/{}".format(UNIT_LED, task_id), payload)

    def _publish_led_state(self, force=False):
        state = self._ism_led.state
        if state == State.ON:
            values = self._last_on
        elif state == State.BLINKING:
            values = self._blink_config[:3] + (255, self._blink_config[3])
        else:
            values = (0, 0, 0, 0)
        snapshot = (state,) + values
        if not force and snapshot == self._last_led_snapshot:
            return
        payload = {"unit_id": UNIT_LED,
                   "ism": state, "r": values[0], "g": values[1],
                   "b": values[2], "brightness": values[3], "ts": time.time()}
        if state == State.BLINKING:
            payload["count"] = values[4]
        self._mqtt.publish("ssm/agents/{}/state".format(UNIT_LED), payload, retain=True)
        self._last_led_snapshot = snapshot
