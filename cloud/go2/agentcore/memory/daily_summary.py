"""每日情节摘要：懒触发 LLM 生成、持久化、按日检索。"""
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from cloud.go2.agentcore.memory.episode import EPISODES_DB
from cloud.go2.agentcore.tools.tools import get_text_llm
from cloud.go2.agentcore.soul import get_system_prompt

logger = logging.getLogger(__name__)


def _get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    c = sqlite3.connect(db_path or EPISODES_DB)
    c.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            event_type TEXT NOT NULL,
            content    TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_summaries (
            date          TEXT PRIMARY KEY,
            summary       TEXT NOT NULL,
            generated_at  REAL NOT NULL,
            episode_count INTEGER DEFAULT 0
        )
    """)
    c.commit()
    return c


def get_summary(date_str: str, db_path: Optional[Path] = None) -> Optional[str]:
    """同步读取指定日期的摘要，不存在则返回 None。"""
    with _get_conn(db_path) as c:
        row = c.execute(
            "SELECT summary FROM daily_summaries WHERE date = ?", (date_str,)
        ).fetchone()
    return row[0] if row else None


def _get_episodes_for_date(date_str: str, db_path: Optional[Path] = None) -> list[str]:
    """从 episodes 表读取指定日期的所有记录，格式化为带时间标签的文本列表。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    day_start = datetime(d.year, d.month, d.day).timestamp()
    day_end = day_start + 86400
    with _get_conn(db_path) as c:
        rows = c.execute(
            "SELECT ts, content FROM episodes WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
            (day_start, day_end),
        ).fetchall()
    return [
        f"[{datetime.fromtimestamp(ts).strftime('%H:%M')}] {content}"
        for ts, content in rows
    ]


async def generate_and_save(
    date_str: str, db_path: Optional[Path] = None
) -> Optional[str]:
    """调用 LLM 生成指定日期的摘要并写入数据库，无记录则返回 None。"""
    episodes = _get_episodes_for_date(date_str, db_path)
    if not episodes:
        return None

    from langchain_core.messages import HumanMessage, SystemMessage

    episodes_text = "\n".join(episodes)
    prompt = (
        f"以下是机器狗 {date_str} 的完整事件记录：\n\n"
        f"{episodes_text}\n\n"
        f"请用第一人称生成一份简洁的当日摘要，涵盖：\n"
        f"1. 探索的地点和路线\n"
        f"2. 遇到的人（外貌或行为描述）\n"
        f"3. 执行的动作和与用户的交互\n"
        f"4. 其他值得记录的事项\n\n"
        f"要求：简洁，不超过 200 字，不要有编号或标题。"
    )
    try:
        resp = await get_text_llm().ainvoke([
            SystemMessage(content=get_system_prompt()),
            HumanMessage(content=prompt),
        ])
        summary = resp.content.strip()
    except Exception as exc:
        logger.warning("[DailySummary] 生成失败 %s: %s", date_str, exc)
        return None

    with _get_conn(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO daily_summaries "
            "(date, summary, generated_at, episode_count) VALUES (?, ?, ?, ?)",
            (date_str, summary, time.time(), len(episodes)),
        )
    logger.info("[DailySummary] 已生成 %s 摘要（%d 条记录）", date_str, len(episodes))
    return summary


def get_recent_summaries(n_days: int = 6, db_path: Optional[Path] = None) -> list[dict]:
    """返回最近 n_days 天（不含今天）已存在的摘要，按时间倒序。"""
    today = date.today()
    result = []
    for i in range(1, n_days + 1):
        d = today - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        summary = get_summary(date_str, db_path)
        if summary:
            result.append({"date": date_str, "summary": summary})
    return result


async def ensure_yesterday_summary(db_path: Optional[Path] = None) -> None:
    """懒触发：若昨天有 episode 记录但无摘要，则自动生成，不阻塞调用方。"""
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    if get_summary(yesterday, db_path) is not None:
        return
    if not _get_episodes_for_date(yesterday, db_path):
        return
    logger.info("[DailySummary] 昨天（%s）缺少摘要，后台生成中...", yesterday)
    await generate_and_save(yesterday, db_path)
