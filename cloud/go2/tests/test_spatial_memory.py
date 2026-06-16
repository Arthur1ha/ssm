import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import pytest
from unittest.mock import AsyncMock, patch
import cloud.go2.agentcore.memory.spatial as sm


@pytest.fixture(autouse=True)
def tmp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "SPATIAL_FILE", tmp_path / "spatial.json")


def test_tag_and_list_location():
    odom = {"x": 1.5, "y": -0.3, "heading": 0.0}
    result = sm.tag_location("门口", odom)
    assert "门口" in result

    locs = sm.list_locations()
    assert len(locs) == 1
    assert locs[0]["name"] == "门口"
    assert locs[0]["x"] == pytest.approx(1.5)


def test_tag_overwrites_same_name():
    sm.tag_location("门口", {"x": 1.0, "y": 0.0, "heading": 0.0})
    sm.tag_location("门口", {"x": 2.0, "y": 0.0, "heading": 0.0})
    locs = sm.list_locations()
    assert len(locs) == 1
    assert locs[0]["x"] == pytest.approx(2.0)


def test_find_location_exact_match():
    sm.tag_location("窗边", {"x": 3.0, "y": 1.0, "heading": 1.57})
    result = asyncio.run(sm.find_location("窗边"))
    assert result is not None
    assert result["x"] == pytest.approx(3.0)


def test_find_location_returns_none_for_missing():
    result = asyncio.run(sm.find_location("不存在的地方"))
    assert result is None


def test_find_location_llm_fallback():
    sm.tag_location("讲台", {"x": 0.5, "y": 0.0, "heading": 0.0})
    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(return_value=AsyncMock(content="讲台"))
    with patch.object(sm, "_get_text_llm", return_value=mock_llm):
        result = asyncio.run(sm.find_location("站台"))
    assert result is not None
    assert result["name"] == "讲台"


def test_delete_location():
    sm.tag_location("黑板", {"x": 0.0, "y": 0.0, "heading": 0.0})
    assert sm.delete_location("黑板") is True
    assert sm.delete_location("黑板") is False
    assert sm.list_locations() == []


def test_no_record_trajectory_tick():
    assert not hasattr(sm, "record_trajectory_tick")
