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


import asyncio
from unittest.mock import AsyncMock, patch, MagicMock


# --- evolve_from_summary ---

@pytest.mark.asyncio
async def test_evolve_from_summary_updates_traits_and_saves(tmp_path):
    from cloud.go2.agentcore.soul_evolution import evolve_from_summary

    traits_file = tmp_path / "traits.json"
    traits_file.write_text(json.dumps({
        "curiosity": 70, "extraversion": 45, "boldness": 60, "last_evolved": None
    }))

    mock_resp = MagicMock()
    mock_resp.content = '{"curiosity": 3, "extraversion": 5, "boldness": 0}'

    with patch("cloud.go2.agentcore.soul_evolution.get_text_llm") as mock_llm_fn, \
         patch("cloud.go2.agentcore.soul_evolution._regen_prompt", new_callable=AsyncMock):
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_resp)
        mock_llm_fn.return_value = mock_llm

        result = await evolve_from_summary("2026-06-07", "今天探索了走廊，遇到两个陌生人。", traits_path=traits_file)

    assert result["curiosity"] == 73
    assert result["extraversion"] == 50
    assert result["boldness"] == 60
    assert result["last_evolved"] == "2026-06-07"

    saved = json.loads(traits_file.read_text())
    assert saved["curiosity"] == 73
    assert saved["last_evolved"] == "2026-06-07"


@pytest.mark.asyncio
async def test_evolve_from_summary_calls_regen_prompt(tmp_path):
    from cloud.go2.agentcore.soul_evolution import evolve_from_summary

    traits_file = tmp_path / "traits.json"
    traits_file.write_text(json.dumps({
        "curiosity": 70, "extraversion": 45, "boldness": 60, "last_evolved": None
    }))

    mock_resp = MagicMock()
    mock_resp.content = '{"curiosity": 0, "extraversion": 0, "boldness": 0}'

    with patch("cloud.go2.agentcore.soul_evolution.get_text_llm") as mock_llm_fn, \
         patch("cloud.go2.agentcore.soul_evolution._regen_prompt", new_callable=AsyncMock) as mock_regen:
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_resp)
        mock_llm_fn.return_value = mock_llm

        await evolve_from_summary("2026-06-07", "平淡的一天。", traits_path=traits_file)

        mock_regen.assert_called_once()


@pytest.mark.asyncio
async def test_evolve_from_summary_handles_llm_parse_failure(tmp_path):
    from cloud.go2.agentcore.soul_evolution import evolve_from_summary

    traits_file = tmp_path / "traits.json"
    traits_file.write_text(json.dumps({
        "curiosity": 70, "extraversion": 45, "boldness": 60, "last_evolved": None
    }))

    mock_resp = MagicMock()
    mock_resp.content = "我不知道怎么回答"  # 非 JSON

    with patch("cloud.go2.agentcore.soul_evolution.get_text_llm") as mock_llm_fn, \
         patch("cloud.go2.agentcore.soul_evolution._regen_prompt", new_callable=AsyncMock):
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_resp)
        mock_llm_fn.return_value = mock_llm

        result = await evolve_from_summary("2026-06-07", "一段摘要", traits_path=traits_file)

    assert result["curiosity"] == 70
    assert result["last_evolved"] == "2026-06-07"
