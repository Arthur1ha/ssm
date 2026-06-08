import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from cloud.go2.agentcore.soul_evolution import _load_traits, _save_traits, _apply_delta


def test_load_traits_returns_defaults_when_no_file(tmp_path):
    traits = _load_traits(tmp_path / "missing.json")
    assert traits["curiosity"] == 70
    assert traits["extraversion"] == 45
    assert traits["boldness"] == 60
    assert traits["last_evolved"] is None


def test_load_traits_reads_from_file(tmp_path):
    f = tmp_path / "traits.json"
    f.write_text(json.dumps({"curiosity": 80, "extraversion": 60, "boldness": 55, "last_evolved": "2026-06-07"}))
    traits = _load_traits(f)
    assert traits["curiosity"] == 80
    assert traits["last_evolved"] == "2026-06-07"


def test_apply_delta_updates_traits():
    traits = {"curiosity": 70, "extraversion": 45, "boldness": 60, "last_evolved": None}
    result = _apply_delta(traits, {"curiosity": 3, "extraversion": -2})
    assert result["curiosity"] == 73
    assert result["extraversion"] == 43
    assert result["boldness"] == 60


def test_apply_delta_clamps_to_zero():
    traits = {"curiosity": 2, "extraversion": 45, "boldness": 60, "last_evolved": None}
    result = _apply_delta(traits, {"curiosity": -10})
    assert result["curiosity"] == 0


def test_apply_delta_clamps_to_hundred():
    traits = {"curiosity": 98, "extraversion": 45, "boldness": 60, "last_evolved": None}
    result = _apply_delta(traits, {"curiosity": 10})
    assert result["curiosity"] == 100


def test_apply_delta_ignores_unknown_keys():
    traits = {"curiosity": 70, "extraversion": 45, "boldness": 60, "last_evolved": None}
    result = _apply_delta(traits, {"unknown_trait": 99})
    assert "unknown_trait" not in result
    assert result["curiosity"] == 70
