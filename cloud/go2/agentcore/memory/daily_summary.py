"""每日情节摘要：懒触发 LLM 生成、持久化为 Markdown、按日检索。"""
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from cloud.go2.paths import MEMORY_DIR
from cloud.go2.agentcore.memory.episode import read_day
from cloud.go2.agentcore.tools.tools import get_text_llm
from cloud.go2.agentcore.prompts import build_prompt
from cloud.go2.execution import can_persist, require_persist

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
    """调用 LLM 生成指定日期的摘要并写入 Markdown，无记录则返回 None。"""
    require_persist("write a daily summary")
    episodes = _get_episodes_for_date(date_str, episodes_dir)
    if not episodes:
        return None

    from langchain_core.messages import HumanMessage, SystemMessage
    from cloud.go2.agentcore.soul import get_system_prompt

    # Keep the start and end of a busy day; never send unbounded JSONL to LLM.
    if len(episodes) > _SUMMARY_INPUT_EVENTS:
        episodes = episodes[:10] + episodes[-(_SUMMARY_INPUT_EVENTS - 10):]
    per_event_limit = max(40, _SUMMARY_INPUT_CHARS // len(episodes) - 1)
    episodes_text = "\n".join(line[:per_event_limit] for line in episodes)[:_SUMMARY_INPUT_CHARS]
    bundle = build_prompt(
        "daily_summary",
        personality=get_system_prompt(),
        user_input=f"请概括来源日期 {date_str} 的记录。",
        memory=episodes_text,
        memory_sources=(f"episode:{date_str}",),
        budget_chars=7000,
    )
    try:
        resp = await get_text_llm().ainvoke([
            SystemMessage(content=bundle.system),
            HumanMessage(content=bundle.human),
        ])
        summary = resp.content.strip()
    except Exception as exc:
        logger.warning("[DailySummary] 生成失败 %s: %s", date_str, exc)
        return None

    f = _summary_file(date_str, base_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"<!-- source_date: {date_str} -->\n{summary}", encoding="utf-8")
    logger.info("[DailySummary] 已生成 %s 摘要（%d 条记录）", date_str, len(episodes))
    return summary


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
