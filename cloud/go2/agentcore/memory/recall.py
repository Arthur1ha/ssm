"""Bounded, source-labelled memory selection for Go2 conversations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping

from cloud.go2.agentcore.memory import daily_summary
from cloud.go2.agentcore.memory.episode import EpisodeMemory, EventType, _RETENTION_DAYS, episode_memory, read_day

DEFAULT_BUDGET_CHARS = 2400
MAX_TODAY_EVENTS = 12
MAX_SUMMARIES = 3
MAX_HISTORY_TURNS = 6
_DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")
_CHINESE_DATE_RE = re.compile(r"(?:(20\d{2})年)?(1[0-2]|0?[1-9])月(3[01]|[12]\d|0?[1-9])日?")
_HISTORY_WORDS = ("今天", "昨天", "前天", "发生", "做了", "做过", "去过", "去哪", "哪里", "哪儿", "记得", "历史", "最近")
_LOCATION_WORDS = ("去过", "去哪", "地点", "位置", "路线", "探索", "走廊", "房间")
_CURRENT_WORDS = ("当前位置", "现在在哪", "目前在哪", "现在位置")
_STOP_WORDS = set("的了吗呢是我你他她它在有今天昨天最近什么事情哪里哪儿做了发生过去现在请问一下")


@dataclass(frozen=True)
class MemoryFragment:
    source: str
    date_range: str
    text: str
    event_type: str = ""

    def render(self) -> str:
        kind = f"/{self.event_type}" if self.event_type else ""
        return f"[历史数据 {self.source}{kind} {self.date_range}] {self.text}"


@dataclass(frozen=True)
class RecallResult:
    fragments: tuple[MemoryFragment, ...]
    budget_chars: int
    unknown: bool = False

    @property
    def text(self) -> str:
        if self.fragments:
            body = "\n".join(part.render() for part in self.fragments)
            marker = "\n（无相关历史记忆）"
            return body + marker if self.unknown and len(body) + len(marker) <= self.budget_chars else body
        return "（无相关记忆）"[:max(0, self.budget_chars)]

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(part.source for part in self.fragments))


def _question_dates(question: str, today: date) -> tuple[str | None, bool]:
    explicit = _DATE_RE.search(question)
    if explicit:
        try:
            return date.fromisoformat(explicit.group()).isoformat(), True
        except ValueError:
            return None, True
    chinese_date = _CHINESE_DATE_RE.search(question)
    if chinese_date:
        year, month, day = chinese_date.groups()
        try:
            selected = date(int(year or today.year), int(month), int(day))
            if year is None and selected > today:
                selected = selected.replace(year=selected.year - 1)
            return selected.isoformat(), True
        except ValueError:
            return None, True
    if "前天" in question:
        return (today - timedelta(days=2)).isoformat(), True
    if "昨天" in question:
        return (today - timedelta(days=1)).isoformat(), True
    if "今天" in question:
        return today.isoformat(), True
    return None, False


def _relevance(question: str, content: str) -> int:
    query = {question[i:i + 2] for i in range(len(question) - 1)}
    query = {term for term in query if not all(char in _STOP_WORDS for char in term)}
    return sum(1 for term in query if term in content)


def _fit(fragments: Iterable[MemoryFragment], budget_chars: int, limit: int, *, unknown: bool = False) -> RecallResult:
    chosen: list[MemoryFragment] = []
    used = 0
    for part in fragments:
        if len(chosen) >= limit:
            break
        remaining = budget_chars - used - (1 if chosen else 0)
        if remaining <= 0:
            break
        prefix = part.render()[:len(part.render()) - len(part.text)]
        if len(prefix) >= remaining:
            continue
        content = part.text[:min(400, remaining - len(prefix))]
        fitted = MemoryFragment(part.source, part.date_range, content, part.event_type)
        chosen.append(fitted)
        used += len(fitted.render()) + (1 if len(chosen) > 1 else 0)
    return RecallResult(tuple(chosen), budget_chars, unknown)


def recall_memory(
    question: str,
    *,
    actor: str,
    session_id: str,
    session_history: Iterable[Mapping[str, str]] = (),
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    episode_store: EpisodeMemory | None = None,
    summaries_dir: Path | None = None,
    current_state: str = "",
    today: date | None = None,
    persist: bool | None = None,
) -> RecallResult:
    """Select memory without crossing the visitor's session boundary.

    ``session_history`` must be the history belonging to ``session_id``. The
    function never reads the process-wide histories of other sessions.
    """
    if budget_chars <= 0:
        return RecallResult((), budget_chars)
    today = today or date.today()
    can_read_persistent = actor == "admin" and persist is not False
    fragments: list[MemoryFragment] = []

    selected_date, date_requested = _question_dates(question, today)
    history_question = date_requested or any(word in question for word in _HISTORY_WORDS)
    current_question = any(word in question for word in _CURRENT_WORDS)
    if current_question:
        if current_state:
            fragments.append(MemoryFragment("current_state", today.isoformat(), current_state))
        return _fit(fragments, budget_chars, 1)

    if not can_read_persistent:
        rows = list(session_history)[-MAX_HISTORY_TURNS:]
        for row in reversed(rows):
            role = row.get("role", "unknown")
            content = row.get("content", "")
            if content:
                fragments.append(MemoryFragment(f"session:{session_id}", today.isoformat(), f"{role}: {content}"))
        has_history = bool(fragments)
        if current_state:
            fragments.append(MemoryFragment("current_state", today.isoformat(), current_state))
        return _fit(fragments, budget_chars, MAX_HISTORY_TURNS + 1, unknown=history_question and not has_history)

    store = episode_store or episode_memory
    if date_requested and selected_date is None:
        if current_state:
            fragments.append(MemoryFragment("current_state", today.isoformat(), current_state))
        return _fit(fragments, budget_chars, 1, unknown=True)
    location_question = any(word in question for word in _LOCATION_WORDS)

    day = selected_date or today.isoformat()
    source_date = date.fromisoformat(day)
    rows = read_day(day, store.episodes_dir) if source_date >= today - timedelta(days=_RETENTION_DAYS) else []
    wanted_types = (
        {EventType.OBSERVATION.value, EventType.ACTION_TAKEN.value, EventType.VISION_CHANGE.value}
        if location_question else
        {EventType.ACTION_TAKEN.value, EventType.OBSERVATION.value, EventType.IDENTITY_CHANGE.value,
         EventType.VISION_CHANGE.value, EventType.USER_COMMAND.value, EventType.AGENT_RESPONSE.value}
        if history_question else
        {EventType.ACTION_TAKEN.value, EventType.OBSERVATION.value, EventType.IDENTITY_CHANGE.value}
    )
    candidates = [row for row in rows if row.get("event_type") in wanted_types]
    candidates.sort(key=lambda row: (_relevance(question, row.get("content", "")), row.get("ts", 0)), reverse=True)
    event_limit = MAX_TODAY_EVENTS if history_question else 3
    if history_question:
        if selected_date and selected_date != today.isoformat():
            summary = daily_summary.get_summary(selected_date, summaries_dir)
            summaries = [{"date": selected_date, "summary": summary}] if summary else []
        elif selected_date == today.isoformat():
            summaries = []
        else:
            summaries = daily_summary.get_recent_summaries(6, summaries_dir)
            summaries.sort(key=lambda item: (_relevance(question, item["summary"]), item["date"]), reverse=True)
        for item in summaries[:MAX_SUMMARIES]:
            fragments.append(MemoryFragment("daily_summary", item["date"], item["summary"]))

    for row in candidates[:event_limit]:
        ts = datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M")
        fragments.append(MemoryFragment("episode", ts, row.get("content", ""), row.get("event_type", "")))

    has_history = bool(fragments)
    if current_state:
        fragments.append(MemoryFragment("current_state", today.isoformat(), current_state))
    return _fit(
        fragments, budget_chars, MAX_TODAY_EVENTS + MAX_SUMMARIES + 1,
        unknown=history_question and not has_history,
    )
