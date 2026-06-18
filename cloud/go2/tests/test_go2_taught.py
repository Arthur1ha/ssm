"""Go2 调教记录富 schema 与旧记录迁移。"""
from cloud.go2.agentcore.tools import tools


def test_add_rule_rich_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "RULES_FILE", tmp_path / "rules.json")
    msg = tools.go2_add_rule("人", "Hello", cooldown_s=20, behavior="去打招呼")
    assert "已添加" in msg
    rules = tools.load_rules()
    r = rules[0]
    assert r["trigger"] == "人"
    assert r["action"] == "Hello"
    assert r["behavior"] == "去打招呼"
    assert r["enabled"] is True
    assert r["hit_count"] == 0
    assert r["id"].startswith("tb_")


def test_migrate_old_rule():
    old = {"trigger": "猫", "action": "Dance1", "cooldown_s": 30, "last_triggered": 0}
    m = tools._migrate_rule(old)
    assert m["behavior"] == "Dance1"
    assert m["enabled"] is True
    assert m["hit_count"] == 0
    assert "id" in m
