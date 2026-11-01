"""Persistent Go2 visual-trigger rules."""
import json
import time
from cloud.go2.paths import RULES_FILE
from cloud.go2.execution import require_persist

# ── 规则存储 ──────────────────────────────────────────────────────


def load_rules(*, rules_file=None) -> list:
    rules_file = rules_file or RULES_FILE
    if not rules_file.exists():
        return []
    return json.loads(rules_file.read_text(encoding="utf-8"))


def save_rules(rules: list, *, rules_file=None) -> None:
    require_persist("modify trained rules")
    rules_file = rules_file or RULES_FILE
    rules_file.parent.mkdir(parents=True, exist_ok=True)
    rules_file.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _migrate_rule(rule: dict) -> dict:
    """把旧记录 {trigger, action, cooldown_s, last_triggered} 补全为富 schema。"""
    rule.setdefault("id", f"tb_{int(time.time() * 1000)}")
    rule.setdefault("behavior", rule.get("action", ""))
    rule.setdefault("action_hint", {"skill": rule.get("action", "")})
    rule.setdefault("enabled", True)
    rule.setdefault("created_ts", time.time())
    rule.setdefault("hit_count", 0)
    rule.setdefault("last_fired_ts", None)
    return rule


_NEGATION_PREFIXES = ("没有", "无", "没", "看不到", "未发现", "不见")


def _is_negated(trigger: str, text: str) -> bool:
    for prefix in _NEGATION_PREFIXES:
        if prefix + trigger in text:
            return True
    return False


def check_rules(observation: str, *, include_rules: bool = False, rules_file=None) -> list:
    """Match and reserve cooldown; successful execution is recorded separately."""
    rules = [_migrate_rule(r) for r in load_rules(rules_file=rules_file)]
    now = time.time()
    triggered: list[str] = []
    changed = False
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        trigger = rule["trigger"]
        if trigger in observation and not _is_negated(trigger, observation):
            if now - rule.get("last_triggered", 0) >= rule["cooldown_s"]:
                rule["last_triggered"] = now
                triggered.append(dict(rule) if include_rules else rule["action"])
                changed = True
    if changed:
        save_rules(rules, rules_file=rules_file)
    return triggered


def record_rule_completion(rule_id: str, *, rules_file=None) -> None:
    rules = load_rules(rules_file=rules_file)
    for rule in rules:
        if rule.get("id") == rule_id:
            rule["hit_count"] = rule.get("hit_count", 0) + 1
            rule["last_fired_ts"] = time.time()
            save_rules(rules, rules_file=rules_file)
            return


