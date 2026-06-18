"""check_rules 的 enabled 过滤与命中计数。"""
import json
import time
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
    assert isinstance(r["last_fired_ts"], float)


def test_cooldown_blocks_hit(monkeypatch, tmp_path):
    f = tmp_path / "rules.json"
    monkeypatch.setattr(tools, "RULES_FILE", f)
    _seed(f, [{"id": "tb_1", "trigger": "人", "action": "Hello", "behavior": "",
               "cooldown_s": 99, "last_triggered": time.time() - 1, "enabled": True,
               "hit_count": 0, "last_fired_ts": None}])

    # 冷却未到 → 不触发、不计数
    assert tools.check_rules("人") == []
    r = tools.load_rules()[0]
    assert r["hit_count"] == 0
    assert r["last_fired_ts"] is None


def test_disabled_rule_skipped(monkeypatch, tmp_path):
    f = tmp_path / "rules.json"
    monkeypatch.setattr(tools, "RULES_FILE", f)
    _seed(f, [{"id": "tb_1", "trigger": "人", "action": "Hello", "behavior": "",
               "cooldown_s": 0, "last_triggered": 0, "enabled": False,
               "hit_count": 0, "last_fired_ts": None}])
    assert tools.check_rules("人") == []
