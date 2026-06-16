# cloud/go2/tests/test_paths.py
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


def test_default_data_dir_under_go2(monkeypatch):
    monkeypatch.delenv("GO2_DATA_DIR", raising=False)
    import cloud.go2.paths as paths
    importlib.reload(paths)
    assert paths.DATA_DIR.name == "data"
    assert paths.DATA_DIR.parent.name == "go2"
    assert paths.MEMORY_DIR == paths.DATA_DIR / "memory"
    assert paths.SOUL_DIR == paths.DATA_DIR / "soul"
    assert paths.RULES_FILE == paths.DATA_DIR / "rules.json"


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GO2_DATA_DIR", str(tmp_path / "custom"))
    import cloud.go2.paths as paths
    importlib.reload(paths)
    assert paths.DATA_DIR == tmp_path / "custom"
    assert paths.MEMORY_DIR == tmp_path / "custom" / "memory"
    monkeypatch.delenv("GO2_DATA_DIR")
    importlib.reload(paths)
