# ism.py — Interface State Machine
# Each unit type has its own transition table. No hardware, no MQTT.

# ── States ────────────────────────────────────────────────────
class State:
    SAMPLING   = "SAMPLING"     # light/IR sensor active
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
    SENSOR_FAIL      = "SENSOR_FAIL"
    SENSOR_RECOVERED = "SENSOR_RECOVERED"
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

# ── Per-unit transition tables ────────────────────────────────

SENSOR_TABLE = {
    (State.SAMPLING,   Trigger.SENSOR_FAIL):      State.ERROR,
    (State.MONITORING, Trigger.SENSOR_FAIL):      State.ERROR,
    (State.ERROR,      Trigger.SENSOR_RECOVERED): State.SAMPLING,
    (State.ERROR,      Trigger.CMD_RESET):        State.SAMPLING,
}

LED_TABLE = {
    # From OFF
    (State.OFF,    Trigger.CMD_OFF):   State.OFF,    # 幂等：已关灯时再关无副作用
    (State.OFF,    Trigger.CMD_BRIGHT): State.BRIGHT,
    (State.OFF,    Trigger.CMD_DIM):    State.DIM,
    (State.OFF,    Trigger.CMD_COLOR):  State.COLOR,
    (State.OFF,    Trigger.CMD_BLINK):  State.BLINK,
    # From BRIGHT
    (State.BRIGHT, Trigger.CMD_OFF):    State.OFF,
    (State.BRIGHT, Trigger.CMD_DIM):    State.DIM,
    (State.BRIGHT, Trigger.CMD_COLOR):  State.COLOR,
    (State.BRIGHT, Trigger.CMD_BLINK):  State.BLINK,
    # From DIM
    (State.DIM,    Trigger.CMD_OFF):    State.OFF,
    (State.DIM,    Trigger.CMD_BRIGHT): State.BRIGHT,
    (State.DIM,    Trigger.CMD_COLOR):  State.COLOR,
    (State.DIM,    Trigger.CMD_BLINK):  State.BLINK,
    # From COLOR
    (State.COLOR,  Trigger.CMD_OFF):    State.OFF,
    (State.COLOR,  Trigger.CMD_BRIGHT): State.BRIGHT,
    (State.COLOR,  Trigger.CMD_DIM):    State.DIM,
    (State.COLOR,  Trigger.CMD_COLOR):  State.COLOR,
    (State.COLOR,  Trigger.CMD_BLINK):  State.BLINK,
    # From BLINK — any command interrupts the blink
    (State.BLINK,  Trigger.CMD_OFF):    State.OFF,
    (State.BLINK,  Trigger.CMD_BRIGHT): State.BRIGHT,
    (State.BLINK,  Trigger.CMD_DIM):    State.DIM,
    (State.BLINK,  Trigger.CMD_COLOR):  State.COLOR,
    (State.BLINK,  Trigger.BLINK_DONE): State.OFF,
}

BUZZER_TABLE = {
    # From SILENT
    (State.SILENT, Trigger.PLAY_ALERT):  State.ALERT,
    (State.SILENT, Trigger.PLAY_NOTIFY): State.NOTIFY,
    # From ALERT — natural end, explicit stop, or interrupt/re-trigger
    (State.ALERT,  Trigger.SOUND_DONE):  State.SILENT,
    (State.ALERT,  Trigger.STOP_SOUND):  State.SILENT,
    (State.ALERT,  Trigger.PLAY_ALERT):  State.ALERT,
    (State.ALERT,  Trigger.PLAY_NOTIFY): State.NOTIFY,
    # From NOTIFY — natural end, explicit stop, or interrupt/re-trigger
    (State.NOTIFY, Trigger.SOUND_DONE):  State.SILENT,
    (State.NOTIFY, Trigger.STOP_SOUND):  State.SILENT,
    (State.NOTIFY, Trigger.PLAY_NOTIFY): State.NOTIFY,
    (State.NOTIFY, Trigger.PLAY_ALERT):  State.ALERT,
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
