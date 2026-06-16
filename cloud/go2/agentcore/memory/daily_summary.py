"""每日情节摘要：懒触发 LLM 生成、持久化为 Markdown、按日检索。"""
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from cloud.go2.paths import MEMORY_DIR
from cloud.go2.agentcore.memory.episode import read_day
from cloud.go2.agentcore.tools.tools import get_text_llm

logger = logging.getLogger(__name__)

_SUMMARIES_DIR = MEMORY_DIR / "summaries"


def _summary_file(date_str: str, base_dir: Optional[Path] = None) -> Path:
    d = Path(base_dir) if base_dir else _SUMMARIES_DIR
    return d / f"{date_str}.md"


def get_summary(date_str: str, base_dir: Optional[Path] = None) -> Optional[str]:
    """读取指定日期摘要 Markdown，不存在或为空返回 None。"""
    f = _summary_file(date_str, base_dir)
    if not f.exists():
        return None
    text = f.read_text(encoding="utf-8").strip()
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
    episodes = _get_episodes_for_date(date_str, episodes_dir)
    if not episodes:
        return None

    from langchain_core.messages import HumanMessage, SystemMessage
    from cloud.go2.agentcore.soul import get_system_prompt

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

    f = _summary_file(date_str, base_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(summary, encoding="utf-8")
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
    """懒触发：若昨天有 episode 记录但无摘要，则自动生成，不阻塞调用方。"""
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    if get_summary(yesterday, base_dir) is not None:
        return
    if not _get_episodes_for_date(yesterday, episodes_dir):
        return
    logger.info("[DailySummary] 昨天（%s）缺少摘要，后台生成中...", yesterday)
    await generate_and_save(yesterday, episodes_dir=episodes_dir, base_dir=base_dir)
