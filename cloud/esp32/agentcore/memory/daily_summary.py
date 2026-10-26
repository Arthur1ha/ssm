"""Daily summary generated only from recorded lamp events."""

import datetime
import json
import threading

from cloud.esp32.paths import EPISODES_FILE, SUMMARY_DIR

_lock = threading.RLock()


def summarize_day(day: str | None = None) -> str:
    day = day or datetime.date.today().isoformat()
    if not EPISODES_FILE.exists():
        return ""
    with _lock:
        entries = []
        for line in EPISODES_FILE.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if datetime.date.fromtimestamp(item["ts"]).isoformat() == day:
                    entries.append(item)
            except (ValueError, KeyError, TypeError):
                continue
        if not entries:
            return ""
        summary = "；".join(
            f"{item['kind']}：{item['detail']}（{item['result']}）" for item in entries[-20:]
        )[:2000]
        SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
        (SUMMARY_DIR / f"{day}.json").write_text(
            json.dumps({"day": day, "summary": summary}, ensure_ascii=False), encoding="utf-8"
        )
        return summary
