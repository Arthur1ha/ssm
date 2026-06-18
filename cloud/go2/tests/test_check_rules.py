"""check_rules 的 enabled 过滤与命中计数。"""
import json
from cloud.go2.agentcore.tools import tools


def _seed(path, rules):
    path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")


def test_check_rules_counts_hit(monkeypatch, tmp_path):
    f = tmp_path / "rules.json"
    monkeypatch.setattr(tools, "RULES_FILE", f)
    _seed(f, [{"id": "tb_1", "trigger": "人", "action": "Hello", "behavior": "打招呼",
               "cooldown_s": 0, "last_triggered": 0, "enabled": True,
               "hit_count": 0, "last_fired_ts": None}])

    actions = tools.check_rules("人")
    assert actions == ["Hello"]
    r = tools.load_rules()[0]
    assert r["hit_count"] == 1
    assert r["last_fired_ts"] is not None


def test_disabled_rule_skipped(monkeypatch, tmp_path):
    f = tmp_path / "rules.json"
    monkeypatch.setattr(tools, "RULES_FILE", f)
    _seed(f, [{"id": "tb_1", "trigger": "人", "action": "Hello", "behavior": "",
               "cooldown_s": 0, "last_triggered": 0, "enabled": False,
               "hit_count": 0, "last_fired_ts": None}])
    assert tools.check_rules("人") == []
