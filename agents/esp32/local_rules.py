# local_rules.py — Autonomous rules active when phone (decision agent) is absent.
# Suppressed automatically when ssm/decision/active = "true" is received.

from config import AGENT_LED

_LED_CMD = "ssm/agents/{}/command".format(AGENT_LED)


class LocalRules:
    def __init__(self, mqtt):
        self._mqtt           = mqtt
        self._decision_active = False
        self._light_level    = "NORMAL"
        self._ir_presence    = False

    def set_decision_active(self, active: bool):
        self._decision_active = active
        status = "手机决策层" if active else "本地规则"
        print(f"[LocalRules] 控制权: {status}")

    def on_light_event(self, level: str):
        self._light_level = level
        self._evaluate()

    def on_ir_event(self, presence: bool):
        self._ir_presence = presence
        self._evaluate()

    def _evaluate(self):
        if self._decision_active:
            return   # PC/phone decision agent is in charge

        lvl = self._light_level

        # 光线暗 → 暖白（不依赖 IR，光敏单独触发）
        if lvl in ("DIM", "DARK"):
            self._cmd_led({
                "cmd": "SET_COLOR",
                "r": 255, "g": 160, "b": 60, "brightness": 180
            })
        # 光线亮 → 关灯
        elif lvl == "BRIGHT":
            self._cmd_led({"cmd": "SET_STATE", "state": "OFF"})

    def on_sound_event(self):
        """声音检测到 → LED 短闪白色提示（不受 decision_active 抑制，始终响应）"""
        self._mqtt.publish(_LED_CMD,
                           {"cmd": "BLINK", "r": 255, "g": 255, "b": 255, "count": 2})

    def _cmd_led(self, payload):
        self._mqtt.publish(_LED_CMD, payload)
