import sys
import time
import sqlite3
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


def _insert_episode(db: Path, ts: float, content: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS episodes "
                 "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, event_type TEXT, content TEXT)")
    conn.execute("INSERT INTO episodes (ts, event_type, content) VALUES (?, ?, ?)",
                 (ts, "ACTION_TAKEN", content))
    conn.commit()
    conn.close()


def test_get_summary_returns_none_when_no_record(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import get_summary
    assert get_summary("2026-01-01", db_path=tmp_path / "t.db") is None


def test_get_recent_summaries_returns_empty_when_no_data(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import get_recent_summaries
    assert get_recent_summaries(6, db_path=tmp_path / "t.db") == []


def test_get_episodes_for_date_returns_todays_records(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import _get_episodes_for_date
    db = tmp_path / "t.db"
    _insert_episode(db, time.time(), "今天探索了走廊")
    episodes = _get_episodes_for_date(datetime.now().strftime("%Y-%m-%d"), db_path=db)
    assert len(episodes) == 1
    assert "今天探索了走廊" in episodes[0]


def test_get_episodes_for_date_excludes_other_days(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import _get_episodes_for_date
    db = tmp_path / "t.db"
    yesterday_ts = time.time() - 86400
    _insert_episode(db, yesterday_ts, "昨天的事情")
    today_str = datetime.now().strftime("%Y-%m-%d")
    episodes = _get_episodes_for_date(today_str, db_path=db)
    assert all("昨天的事情" not in e for e in episodes)


def test_get_recent_summaries_returns_existing_summaries(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import get_recent_summaries, _get_conn
    db = tmp_path / "t.db"
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    with _get_conn(db) as c:
        c.execute(
            "INSERT INTO daily_summaries (date, summary, generated_at, episode_count) "
            "VALUES (?, ?, ?, ?)",
            (yesterday, "昨天去了走廊，见到一个人。", time.time(), 5),
        )
    result = get_recent_summaries(6, db_path=db)
    assert len(result) == 1
    assert result[0]["date"] == yesterday
    assert "走廊" in result[0]["summary"]


@pytest.mark.asyncio
async def test_generate_and_save_returns_none_when_no_episodes(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import generate_and_save
    result = await generate_and_save("2026-01-01", db_path=tmp_path / "t.db")
    assert result is None


@pytest.mark.asyncio
async def test_generate_and_save_calls_llm_and_persists(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import generate_and_save, get_summary
    db = tmp_path / "t.db"
    today_str = datetime.now().strftime("%Y-%m-%d")
    _insert_episode(db, time.time(), "探索了北边区域")

    mock_resp = AsyncMock()
    mock_resp.content = "今天我探索了北边区域，环境宽敞，没有遇到人。"

    with patch("cloud.go2.agentcore.memory.daily_summary.get_text_llm") as mock_llm_fn, \
         patch("cloud.go2.agentcore.memory.daily_summary.get_system_prompt", return_value="你是机器狗"):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_resp)
        mock_llm_fn.return_value = mock_llm

        result = await generate_and_save(today_str, db_path=db)

    assert result == "今天我探索了北边区域，环境宽敞，没有遇到人。"
    assert get_summary(today_str, db_path=db) == result


@pytest.mark.asyncio
async def test_ensure_yesterday_summary_skips_if_already_exists(tmp_path):
    from cloud.go2.agentcore.memory.daily_summary import ensure_yesterday_summary, _get_conn
    db = tmp_path / "t.db"
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    with _get_conn(db) as c:
        c.execute(
            "INSERT INTO daily_summaries (date, summary, generated_at, episode_count) "
            "VALUES (?, ?, ?, ?)",
            (yesterday, "已有摘要", time.time(), 1),
        )

    with patch("cloud.go2.agentcore.memory.daily_summary.generate_and_save") as mock_gen:
        await ensure_yesterday_summary(db_path=db)
        mock_gen.assert_not_called()
