"""情节记忆：事件流的读写与持久化（按天 JSONL 文件）。"""
import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional, TypedDict

from cloud.go2.paths import MEMORY_DIR

logger = logging.getLogger(__name__)

_EPISODES_DIR = MEMORY_DIR / "episodes"
_RETENTION_DAYS = 7
_BUFFER_SIZE = 20


class EventType(str, Enum):
    VISION_CHANGE  = "VISION_CHANGE"
    ACTION_TAKEN   = "ACTION_TAKEN"
    USER_COMMAND   = "USER_COMMAND"
    OBSERVATION    = "OBSERVATION"
    AGENT_RESPONSE = "AGENT_RESPONSE"


class MemoryEntry(TypedDict):
    ts:         float
    event_type: str
    content:    str


def _fmt_ts(ts: float) -> str:
    """将 Unix 时间戳转为人类可读的相对时间标签。"""
    now = time.time()
    dt = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    diff = now - ts
    if dt.date() == today:
        if diff < 60:
            return f"{int(diff)}秒前"
        if diff < 3600:
            return f"{int(diff // 60)}分钟前"
        return f"今天 {dt.strftime('%H:%M')}"
    if dt.date() == today - timedelta(days=1):
        return f"昨天 {dt.strftime('%H:%M')}"
    return dt.strftime("%m-%d %H:%M")


def _day_file(date_str: str, episodes_dir: Path) -> Path:
    return episodes_dir / f"{date_str}.jsonl"


def read_day(date_str: str, episodes_dir: Optional[Path] = None) -> list[MemoryEntry]:
    """读取指定日期文件的全部事件，无文件返回空列表。供 daily_summary 调用。"""
    d = Path(episodes_dir) if episodes_dir else _EPISODES_DIR
    f = _day_file(date_str, d)
    if not f.exists():
        return []
    out: list[MemoryEntry] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


class EpisodeMemory:
    """情节记忆：内存缓冲 + 按天 JSONL 持久化，跨会话保留 7 天。"""

    def __init__(self, episodes_dir: Optional[Path] = None) -> None:
        self._dir = Path(episodes_dir) if episodes_dir else _EPISODES_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._buffer: deque[MemoryEntry] = deque(maxlen=_BUFFER_SIZE)
        self._cleanup_old()
        self._load_recent()

    def _cleanup_old(self) -> None:
        cutoff = (datetime.now().date() - timedelta(days=_RETENTION_DAYS))
        deleted = 0
        for f in self._dir.glob("*.jsonl"):
            try:
                d = datetime.strptime(f.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff:
                f.unlink()
                deleted += 1
        if deleted:
            logger.info("[EpisodeMemory] 清理过期文件 %d 个（保留 %d 天）", deleted, _RETENTION_DAYS)

    def _load_recent(self) -> None:
        today = datetime.now().date()
        entries: list[MemoryEntry] = []
        for offset in (1, 0):  # 先昨天再今天，保证时间升序
            date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            entries.extend(read_day(date_str, self._dir))
        for e in entries[-_BUFFER_SIZE:]:
            self._buffer.append(e)
        logger.info("[EpisodeMemory] 加载历史记录 %d 条", len(self._buffer))

    def add(self, event_type: EventType, content: str) -> None:
        """写入一条事件，同时追加到当天 JSONL 文件。"""
        entry: MemoryEntry = {
            "ts":         time.time(),
            "event_type": event_type.value,
            "content":    content,
        }
        self._buffer.append(entry)
        today = datetime.now().strftime("%Y-%m-%d")
        with _day_file(today, self._dir).open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.debug("[EpisodeMemory] +%s %s", event_type.value, content)

    def entries(self) -> list[MemoryEntry]:
        """返回内存缓冲中的最近事件列表。"""
        return list(self._buffer)

    def format_context(self) -> str:
        """最近 N 条事件的文本摘要，供 reactive/drive 即时决策使用。接口不变。"""
        if not self._buffer:
            return "（暂无近期事件）"
        lines = [f"[{_fmt_ts(e['ts'])}] {e['content']}" for e in reversed(self._buffer)]
        return "最近事件（最新在前）：\n" + "\n".join(lines)

    def format_today(self) -> str:
        """今天所有事件的文本摘要，供 agent 回答用户问题使用。"""
        today = datetime.now().strftime("%Y-%m-%d")
        rows = read_day(today, self._dir)
        if not rows:
            return "（今天暂无记录）"
        lines = [f"[{_fmt_ts(r['ts'])}] {r['content']}" for r in rows]
        return "今天的事件记录：\n" + "\n".join(lines)


episode_memory = EpisodeMemory()
