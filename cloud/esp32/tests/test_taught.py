"""灯调教记忆的读写/持久化/计数测试。"""
from cloud.esp32.memory import taught


def test_add_and_list_roundtrip(tmp_path):
    f = tmp_path / "taught.json"
    rule = taught.add("天黑了", "调亮一点", action_hint={"tool": "set_led_state"},
                      path=f)
    assert rule["trigger"] == "天黑了"
    assert rule["behavior"] == "调亮一点"
    assert rule["enabled"] is True
    assert rule["hit_count"] == 0
    assert rule["last_fired_ts"] is None
    assert rule["id"].startswith("tb_")

    rules = taught.list_all(path=f)
    assert len(rules) == 1
    assert rules[0]["id"] == rule["id"]


def test_load_corrupt_returns_empty(tmp_path):
    f = tmp_path / "taught.json"
    f.write_text("not json{", encoding="utf-8")
    assert taught.list_all(path=f) == []


def test_delete(tmp_path):
    f = tmp_path / "taught.json"
    r = taught.add("有人来", "闪一下", path=f)
    assert taught.delete(r["id"], path=f) is True
    assert taught.list_all(path=f) == []
    assert taught.delete("tb_nonexistent", path=f) is False


def test_touch_increments_hit(tmp_path):
    f = tmp_path / "taught.json"
    r = taught.add("天黑了", "调亮", path=f)
    taught.touch(r["id"], path=f)
    taught.touch(r["id"], path=f)
    rule = taught.list_all(path=f)[0]
    assert rule["hit_count"] == 2
    assert rule["last_fired_ts"] is not None
