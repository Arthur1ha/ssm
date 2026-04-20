# ism.py — Interface State Machine
# Each unit type has its own transition table. No hardware, no MQTT.

# ── States ────────────────────────────────────────────────────
class State:
    BOOT       = "BOOT"
    SAMPLING   = "SAMPLING"     # light sensor active
    MONITORING = "MONITORING"   # IR sensor active
    OFF        = "OFF"
    DIM        = "DIM"
    BRIGHT     = "BRIGHT"
    COLOR      = "COLOR"
    BLINK      = "BLINK"
    SILENT     = "SILENT"
    ALERT      = "ALERT"
    NOTIFY     = "NOTIFY"
    ERROR      = "ERROR"

# ── Triggers ──────────────────────────────────────────────────
class Trigger:
    INIT_COMPLETE    = "INIT_COMPLETE"
    SENSOR_FAIL      = "SENSOR_FAIL"
    SENSOR_RECOVERED = "SENSOR_RECOVERED"
    CMD_ON           = "CMD_ON"
    CMD_OFF          = "CMD_OFF"
    CMD_DIM          = "CMD_DIM"
    CMD_BRIGHT       = "CMD_BRIGHT"
    CMD_COLOR        = "CMD_COLOR"
    CMD_BLINK        = "CMD_BLINK"
    CMD_RESET        = "CMD_RESET"
    BLINK_DONE       = "BLINK_DONE"
    PLAY_ALERT       = "PLAY_ALERT"
    PLAY_NOTIFY      = "PLAY_NOTIFY"
    STOP_SOUND       = "STOP_SOUND"
    SOUND_DONE       = "SOUND_DONE"
    HW_FAULT         = "HW_FAULT"

# ── Per-unit transition tables ────────────────────────────────

# Shared by light and IR sensors
SENSOR_TABLE = {
    (State.BOOT,       Trigger.INIT_COMPLETE):    State.SAMPLING,
    (State.SAMPLING,   Trigger.SENSOR_FAIL):      State.ERROR,
    (State.SAMPLING,   Trigger.HW_FAULT):         State.ERROR,
    (State.MONITORING, Trigger.INIT_COMPLETE):    State.MONITORING,
    (State.MONITORING, Trigger.SENSOR_FAIL):      State.ERROR,
    (State.ERROR,      Trigger.SENSOR_RECOVERED): State.SAMPLING,
    (State.ERROR,      Trigger.CMD_RESET):        State.SAMPLING,
}

LED_TABLE = {
    (State.OFF,    Trigger.CMD_ON):     State.BRIGHT,
    (State.OFF,    Trigger.CMD_COLOR):  State.COLOR,
    (State.OFF,    Trigger.CMD_DIM):    State.DIM,
    (State.OFF,    Trigger.CMD_BLINK):  State.BLINK,
    (State.BRIGHT, Trigger.CMD_OFF):   State.OFF,
    (State.BRIGHT, Trigger.CMD_DIM):   State.DIM,
    (State.BRIGHT, Trigger.CMD_COLOR): State.COLOR,
    (State.BRIGHT, Trigger.CMD_BLINK): State.BLINK,
    (State.DIM,    Trigger.CMD_OFF):   State.OFF,
    (State.DIM,    Trigger.CMD_BRIGHT):State.BRIGHT,
    (State.DIM,    Trigger.CMD_COLOR): State.COLOR,
    (State.DIM,    Trigger.CMD_BLINK): State.BLINK,
    (State.COLOR,  Trigger.CMD_OFF):   State.OFF,
    (State.COLOR,  Trigger.CMD_BRIGHT):State.BRIGHT,
    (State.COLOR,  Trigger.CMD_COLOR): State.COLOR,
    (State.COLOR,  Trigger.CMD_BLINK): State.BLINK,
    (State.BLINK,  Trigger.BLINK_DONE):State.OFF,
    (State.BLINK,  Trigger.CMD_OFF):   State.OFF,
}

BUZZER_TABLE = {
    (State.SILENT, Trigger.PLAY_ALERT):  State.ALERT,
    (State.SILENT, Trigger.PLAY_NOTIFY): State.NOTIFY,
    (State.ALERT,  Trigger.SOUND_DONE):  State.SILENT,
    (State.ALERT,  Trigger.STOP_SOUND):  State.SILENT,
    (State.NOTIFY, Trigger.SOUND_DONE):  State.SILENT,
    (State.NOTIFY, Trigger.STOP_SOUND):  State.SILENT,
}


class ISM:
    def __init__(self, initial_state, agent_id, table):
        self.state    = initial_state
        self.agent_id = agent_id
        self._table   = table

    def transition(self, trigger):
        key = (self.state, trigger)
        if key in self._table:
            old = self.state
            self.state = self._table[key]
            print("[ISM:{}] {} --{}--> {}".format(self.agent_id, old, trigger, self.state))
            return True
        print("[ISM:{}] BLOCKED: {} + {}".format(self.agent_id, self.state, trigger))
        return False
