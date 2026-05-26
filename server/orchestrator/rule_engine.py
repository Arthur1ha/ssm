import json
import time
import uuid
from pathlib import Path

RULES_FILE = Path(__file__).parent / "rules.json"

# 语义 event 名 → payload 匹配函数
# 新增传感器类型只需在此表加条目
_EVENT_MATCHERS = {
    ("presence",   "detected"):    lambda p: p.get("presence") is True,
    ("presence",   "disappeared"): lambda p: p.get("presence") is False,
    ("light_level","dark"):        lambda p: p.get("level") in ("DARK", "DIM"),
    ("light_level","bright"):      lambda p: p.get("level") in ("BRIGHT", "NORMAL"),
    ("light_level","changed"):     lambda p: "level" in p,
    ("sound",      "detected"):    lambda p: True,
}


class RuleEngine:
    def __init__(self, shared_state, publish_task_fn):
        self._state = shared_state
        self._publish_task = publish_task_fn  # tools.do_publish_task

    def _load(self):
        try:
            return json.loads(RULES_FILE.read_text()) if RULES_FILE.exists() else []
        except Exception:
            return []

    def _save(self, rules):
        RULES_FILE.write_text(json.dumps(rules, indent=2, ensure_ascii=False))

    def list_rules(self):
        return self._load()

    def add_rule(self, rule: dict) -> dict:
        rules = self._load()
        rule.setdefault("rule_id", f"r_{int(time.time())}_{uuid.uuid4().hex[:6]}")
        rule.setdefault("enabled", True)
        rule.setdefault("created_at", int(time.time()))
        rules.append(rule)
        self._save(rules)
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        rules = self._load()
        new = [r for r in rules if r["rule_id"] != rule_id]
        if len(new) < len(rules):
            self._save(new)
            return True
        return False

    def toggle_rule(self, rule_id: str, enabled: bool) -> bool:
        rules = self._load()
        for r in rules:
            if r["rule_id"] == rule_id:
                r["enabled"] = enabled
                self._save(rules)
                return True
        return False

    def match_and_fire(self, unit_id: str, payload: dict) -> bool:
        """传感器 event 到来时调用。从 manifest 读 agent_tag，匹配规则，直接派发任务。
        返回 True 表示至少一条规则命中（调用方可跳过后续 LLM 决策）。"""
        manifest = self._state.get_manifest(unit_id)
        if not manifest:
            return False
        agent_tag = manifest.get("agent_tag")
        if not agent_tag:
            return False

        rules = self._load()  # 每次热加载，确保 API 保存后立即生效
        registry = self._state.get_capability_registry()
        session_id = f"rule_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        fired = False

        for rule in rules:
            if not rule.get("enabled", True):
                continue
            trigger = rule.get("trigger", {})
            if trigger.get("agent_tag") != agent_tag:
                continue
            event_name = trigger.get("event", "")
            matcher = _EVENT_MATCHERS.get((agent_tag, event_name))
            if matcher is None or not matcher(payload):
                continue

            print(f"[RuleEngine] '{rule.get('name')}' fired ({agent_tag}.{event_name})")
            action = rule.get("action", {})
            resource_tag = action.get("resource_tag", "")
            for device_id in registry.get(resource_tag, []):
                task_id = f"{session_id}_{device_id[:8]}"
                self._publish_task(
                    device_id, task_id,
                    action.get("cmd", "SET_STATE"),
                    action.get("params", {}),
                    session_id,
                )
                print(f"[RuleEngine]   → task dispatched to {device_id}")
            fired = True

        return fired
