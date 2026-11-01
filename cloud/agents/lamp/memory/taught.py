"""Read-only compatibility reader for lamp teachings awaiting migration."""
import json
from pathlib import Path
from cloud.esp32.paths import TAUGHT_FILE


def list_all(path: Path | None = None) -> list:
    source = path or TAUGHT_FILE
    if not source.exists():
        return []
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("legacy lamp teachings must be a list")
    return data
