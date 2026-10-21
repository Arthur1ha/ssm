# ism.py — Interface State Machine
# Each unit type has its own transition table. No hardware, no MQTT.

class State:
    SAMPLING   = "SAMPLING"
    MONITORING = "MONITORING"
    OFF        = "OFF"
    ON         = "ON"
    BLINKING   = "BLINKING"
    ERROR      = "ERROR"

class Trigger:
    SENSOR_FAIL      = "SENSOR_FAIL"
    SENSOR_RECOVERED = "SENSOR_RECOVERED"
    CMD_OFF          = "CMD_OFF"
    CMD_ON           = "CMD_ON"
    CMD_COLOR        = "CMD_COLOR"
    CMD_BLINK        = "CMD_BLINK"
    CMD_RESET        = "CMD_RESET"
    BLINK_DONE       = "BLINK_DONE"

SENSOR_TABLE = {
    (State.SAMPLING,   Trigger.SENSOR_FAIL):      State.ERROR,
    (State.MONITORING, Trigger.SENSOR_FAIL):      State.ERROR,
    (State.ERROR,      Trigger.SENSOR_RECOVERED): State.SAMPLING,
    (State.ERROR,      Trigger.CMD_RESET):        State.SAMPLING,
}

LED_TABLE = {
    (State.OFF,      Trigger.CMD_OFF):   State.OFF,
    (State.OFF,      Trigger.CMD_ON):    State.ON,
    (State.OFF,      Trigger.CMD_COLOR): State.ON,
    (State.OFF,      Trigger.CMD_BLINK): State.BLINKING,
    (State.ON,       Trigger.CMD_OFF):   State.OFF,
    (State.ON,       Trigger.CMD_ON):    State.ON,
    (State.ON,       Trigger.CMD_COLOR): State.ON,
    (State.ON,       Trigger.CMD_BLINK): State.BLINKING,
    (State.BLINKING, Trigger.CMD_OFF):   State.OFF,
    (State.BLINKING, Trigger.CMD_ON):    State.ON,
    (State.BLINKING, Trigger.CMD_COLOR): State.ON,
    (State.BLINKING, Trigger.CMD_BLINK): State.BLINKING,
}


class ISM:
    def __init__(self, initial_state, agent_id, table):
        self.state    = initial_state
        self.agent_id = agent_id
        self._table   = table

    def transition(self, trigger, target=None):
        if trigger == Trigger.BLINK_DONE and self._table is LED_TABLE:
            if self.state != State.BLINKING or target not in (State.OFF, State.ON):
                return False
            old = self.state
            self.state = target
            print("[ISM:{}] {} --{}--> {}".format(self.agent_id, old, trigger, self.state))
            return True
        key = (self.state, trigger)
        if key in self._table:
            old = self.state
            self.state = self._table[key]
            print("[ISM:{}] {} --{}--> {}".format(self.agent_id, old, trigger, self.state))
            return True
        print("[ISM:{}] BLOCKED: {} + {}".format(self.agent_id, self.state, trigger))
        return False

    def allows(self, trigger):
        if trigger == Trigger.BLINK_DONE and self._table is LED_TABLE:
            return self.state == State.BLINKING
        return (self.state, trigger) in self._table
