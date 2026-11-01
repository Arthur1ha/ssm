"""每日情节摘要：懒触发 LLM 生成、持久化为 Markdown、按日检索。"""
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from cloud.go2.paths import MEMORY_DIR
from cloud.agents.go2.memory.episode import read_day
from cloud.go2.tools.tools import get_text_llm
from cloud.go2.execution import can_persist

logger = logging.getLogger(__name__)

_SUMMARIES_DIR = MEMORY_DIR / "summaries"
_SUMMARY_INPUT_CHARS = 5000
_SUMMARY_INPUT_EVENTS = 40
_BACKFILL_DAYS = 7
_BACKFILL_PER_CALL = 1


def _summary_file(date_str: str, base_dir: Optional[Path] = None) -> Path:
    d = Path(base_dir) if base_dir else _SUMMARIES_DIR
    return d / f"{date_str}.md"


def get_summary(date_str: str, base_dir: Optional[Path] = None) -> Optional[str]:
    """读取指定日期摘要 Markdown，不存在或为空返回 None。"""
    f = _summary_file(date_str, base_dir)
    if not f.exists():
        return None
    text = f.read_text(encoding="utf-8").strip()
    if text.startswith("<!-- source_date:"):
        text = text.split("-->", 1)[-1].strip()
    return text or None


def _get_episodes_for_date(
    date_str: str, episodes_dir: Optional[Path] = None
) -> list[str]:
    """读取指定日期的所有事件，格式化为带时间标签的文本列表。"""
    rows = read_day(date_str, episodes_dir)
    return [
        f"[{datetime.fromtimestamp(r['ts']).strftime('%H:%M')}] {r['content']}"
        for r in rows
    ]


async def generate_and_save(
    date_str: str,
    episodes_dir: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> Optional[str]:
    """Run the checkpointed summary Graph and preserve the Markdown API."""
    if not _get_episodes_for_date(date_str, episodes_dir):
        return None
    from cloud.agents.go2.workflows.daily_summary import run_daily_summary_graph
    result = await run_daily_summary_graph(
        date_str, model=get_text_llm(), episodes_dir=episodes_dir, base_dir=base_dir)
    return result.get("summary") if result.get("status") in {"ok", "existing"} else None


def get_recent_summaries(
    n_days: int = 6, base_dir: Optional[Path] = None
) -> list[dict]:
    """返回最近 n_days 天（不含今天）已存在的摘要，按时间倒序。"""
    today = date.today()
    result = []
    for i in range(1, n_days + 1):
        date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        summary = get_summary(date_str, base_dir)
        if summary:
            result.append({"date": date_str, "summary": summary})
    return result


async def ensure_yesterday_summary(
    episodes_dir: Optional[Path] = None, base_dir: Optional[Path] = None
) -> None:
    """Backfill at most one retained day per call, including yesterday."""
    if not can_persist():
        return
    attempts = 0
    for days_ago in range(_BACKFILL_DAYS, 0, -1):
        source_date = (date.today() - timedelta(days=days_ago)).isoformat()
        if get_summary(source_date, base_dir) is not None:
            continue
        if not _get_episodes_for_date(source_date, episodes_dir):
            continue
        logger.info("[DailySummary] 补写 %s 摘要", source_date)
        await generate_and_save(source_date, episodes_dir=episodes_dir, base_dir=base_dir)
        attempts += 1
        if attempts >= _BACKFILL_PER_CALL:
            break
