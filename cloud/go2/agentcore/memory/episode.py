"""情节记忆：事件流的读写与持久化。"""
import sqlite3
import time
from collections import deque
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional, TypedDict

EPISODES_DB = Path(__file__).parent / "episodes.db"
_RETENTION_DAYS = 7
_BUFFER_SIZE = 20


class EventType(str, Enum):
    VISION_CHANGE = "VISION_CHANGE"
    ACTION_TAKEN  = "ACTION_TAKEN"
    USER_COMMAND  = "USER_COMMAND"
    OBSERVATION   = "OBSERVATION"


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
            return f"{int(diff / 60)}分钟前"
        return f"今天 {dt.strftime('%H:%M')}"
    if dt.date() == today - timedelta(days=1):
        return f"昨天 {dt.strftime('%H:%M')}"
    return dt.strftime("%m月%d日 %H:%M")


class EpisodeMemory:
    """情节记忆：内存缓冲 + SQLite 持久化，跨会话保留 7 天。"""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db = db_path or EPISODES_DB
        self._buffer: deque[MemoryEntry] = deque(maxlen=_BUFFER_SIZE)
        self._init_db()
        self._cleanup_old()
        self._load_recent()

    def _get_conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db)
        c.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         REAL NOT NULL,
                event_type TEXT NOT NULL,
                content    TEXT NOT NULL
            )
        """)
        c.commit()
        return c

    def _init_db(self) -> None:
        with self._get_conn():
            pass

    def _cleanup_old(self) -> None:
        cutoff = time.time() - _RETENTION_DAYS * 86400
        with self._get_conn() as c:
            c.execute("DELETE FROM episodes WHERE ts < ?", (cutoff,))

    def _load_recent(self) -> None:
        with self._get_conn() as c:
            rows = c.execute(
                "SELECT ts, event_type, content FROM episodes "
                "ORDER BY ts DESC LIMIT ?",
                (_BUFFER_SIZE,),
            ).fetchall()
        for ts, event_type, content in reversed(rows):
            self._buffer.append({"ts": ts, "event_type": event_type, "content": content})

    def add(self, event_type: EventType, content: str) -> None:
        """写入一条事件，同时持久化到 SQLite。"""
        entry: MemoryEntry = {
            "ts":         time.time(),
            "event_type": event_type.value,
            "content":    content,
        }
        self._buffer.append(entry)
        with self._get_conn() as c:
            c.execute(
                "INSERT INTO episodes (ts, event_type, content) VALUES (?, ?, ?)",
                (entry["ts"], entry["event_type"], entry["content"]),
            )

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
        today_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        with self._get_conn() as c:
            rows = c.execute(
                "SELECT ts, content FROM episodes WHERE ts >= ? ORDER BY ts ASC",
                (today_start,),
            ).fetchall()
        if not rows:
            return "（今天暂无记录）"
        lines = [f"[{_fmt_ts(ts)}] {content}" for ts, content in rows]
        return "今天的事件记录：\n" + "\n".join(lines)


episode_memory = EpisodeMemory()
