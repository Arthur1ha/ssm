import json
import time
from pathlib import Path

RULES_FILE = Path(__file__).parent / "rules.json"


def load_rules() -> list:
    if not RULES_FILE.exists():
        return []
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def save_rules(rules: list) -> None:
    RULES_FILE.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def check_rules(observation: str) -> list[str]:
    rules = load_rules()
    now = time.time()
    triggered = []
    changed = False
    for rule in rules:
        if rule["trigger"] in observation:
            if now - rule.get("last_triggered", 0) >= rule["cooldown_s"]:
                rule["last_triggered"] = now
                triggered.append(rule["action"])
                changed = True
    if changed:
        save_rules(rules)
    return triggered
