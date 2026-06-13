# local_rules.py — Autonomous rules active when decision agent is offline.
# Rules are synced from cloud via ssm/rules/{DEVICE_ID} (retain).
# Suppressed by ssm/decision/active = "true".

import ujson
from config import UNIT_LED

_LED_CMD        = "ssm/agents/{}/command".format(UNIT_LED)
_RULES_CACHE    = "rules_cache.json"
_MAX_RULES      = 10


class LocalRules:
    def __init__(self, mqtt):
        self._mqtt            = mqtt
        self._decision_active = False
        self._rules           = []
        self._load_from_file()

    def set_decision_active(self, active: bool):
        self._decision_active = active
        print("[LocalRules] 控制权: {}".format("云端" if active else "本地规则({} 条)".format(len(self._rules))))

    def load_rules(self, rules):
        """接收云端下推的精简规则列表，缓存到内存和 flash。"""
        self._rules = rules[:_MAX_RULES]
        self._save_to_file(self._rules)
        print("[LocalRules] 规则已更新: {} 条".format(len(self._rules)))

    def _load_from_file(self):
        try:
            with open(_RULES_CACHE, "r") as f:
                self._rules = ujson.loads(f.read())
            print("[LocalRules] 从 flash 加载 {} 条规则".format(len(self._rules)))
        except:
            self._rules = []

    def _save_to_file(self, rules):
        try:
            with open(_RULES_CACHE, "w") as f:
                f.write(ujson.dumps(rules))
        except:
            pass  # flash 写入失败不影响运行

    def on_light_event(self, level: str):
        self._fire("light_level", {"level": level})

    def on_ir_event(self, presence: bool):
        self._fire("presence", {"presence": presence})

    def on_sound_event(self):
        self._fire("sound", {"detected": True})

    def _fire(self, sensor_tag, payload):
        if self._decision_active:
            return
        for rule in self._rules:
            if not rule.get("en", True):
                continue
            trig = rule.get("trig", {})
            if trig.get("tag") != sensor_tag:
                continue
            if self._match(trig.get("ev", ""), sensor_tag, payload):
                act = rule.get("act", {})
                self._cmd_led(act)
                break  # 每次传感器事件只触发第一条匹配规则

    def _match(self, ev, tag, payload):
        if tag == "presence":
            if ev == "detected":    return payload.get("presence") is True
            if ev == "disappeared": return payload.get("presence") is False
        elif tag == "light_level":
            lvl = payload.get("level", "")
            if ev == "dark":    return lvl in ("DARK", "DIM")
            if ev == "bright":  return lvl in ("BRIGHT", "NORMAL")
            if ev == "changed": return "level" in payload
        elif tag == "sound":
            if ev == "detected": return True
        return False

    def _cmd_led(self, payload):
        self._mqtt.publish(_LED_CMD, payload)
