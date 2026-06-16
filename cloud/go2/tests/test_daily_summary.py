# cloud/go2/tests/test_daily_summary.py
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from cloud.go2.agentcore.memory.episode import EpisodeMemory, EventType


def test_get_summary_returns_none_when_no_record(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import get_summary
    assert get_summary("2026-01-01", base_dir=tmp_path) is None


def test_get_recent_summaries_returns_empty_when_no_data(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import get_recent_summaries
    assert get_recent_summaries(6, base_dir=tmp_path) == []


def test_get_episodes_for_date_returns_todays_records(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import _get_episodes_for_date
    EpisodeMemory(episodes_dir=tmp_path).add(EventType.OBSERVATION, "今天探索了走廊")
    today = datetime.now().strftime("%Y-%m-%d")
    episodes = _get_episodes_for_date(today, episodes_dir=tmp_path)
    assert len(episodes) == 1
    assert "今天探索了走廊" in episodes[0]


def test_get_episodes_for_date_excludes_other_days(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import _get_episodes_for_date
    EpisodeMemory(episodes_dir=tmp_path).add(EventType.OBSERVATION, "今天的事情")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    episodes = _get_episodes_for_date(yesterday, episodes_dir=tmp_path)
    assert episodes == []


def test_get_recent_summaries_returns_existing_summaries(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import get_recent_summaries
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    (tmp_path / f"{yesterday}.md").write_text("昨天去了走廊，见到一个人。", encoding="utf-8")
    result = get_recent_summaries(6, base_dir=tmp_path)
    assert len(result) == 1
    assert result[0]["date"] == yesterday
    assert "走廊" in result[0]["summary"]


@pytest.mark.asyncio
async def test_generate_and_save_returns_none_when_no_episodes(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import generate_and_save
    result = await generate_and_save(
        "2026-01-01", episodes_dir=tmp_path / "ep", base_dir=tmp_path / "sum"
    )
    assert result is None


@pytest.mark.asyncio
async def test_generate_and_save_calls_llm_and_persists(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import generate_and_save, get_summary
    ep = tmp_path / "ep"
    sm = tmp_path / "sum"
    EpisodeMemory(episodes_dir=ep).add(EventType.OBSERVATION, "探索了北边区域")
    today = datetime.now().strftime("%Y-%m-%d")

    mock_resp = AsyncMock()
    mock_resp.content = "今天我探索了北边区域，环境宽敞，没有遇到人。"
    with patch("cloud.go2.agentcore.memory.daily_summary.get_text_llm") as mock_llm_fn, \
         patch("cloud.go2.agentcore.soul.get_system_prompt", return_value="你是机器狗"):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_resp)
        mock_llm_fn.return_value = mock_llm
        result = await generate_and_save(today, episodes_dir=ep, base_dir=sm)

    assert result == "今天我探索了北边区域，环境宽敞，没有遇到人。"
    assert get_summary(today, base_dir=sm) == result


@pytest.mark.asyncio
async def test_ensure_yesterday_summary_skips_if_already_exists(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import ensure_yesterday_summary
    sm = tmp_path / "sum"
    sm.mkdir()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    (sm / f"{yesterday}.md").write_text("已有摘要", encoding="utf-8")

    with patch("cloud.go2.agentcore.memory.daily_summary.generate_and_save") as mock_gen:
        await ensure_yesterday_summary(episodes_dir=tmp_path / "ep", base_dir=sm)
        mock_gen.assert_not_called()
