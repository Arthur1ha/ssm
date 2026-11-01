"""One-way, idempotent import of legacy rule files into inert teaching memory."""
from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path

from cloud.agents.common import teachings
from cloud.esp32.paths import TAUGHT_FILE
from cloud.go2.paths import RULES_FILE

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_ESP32_RULES = Path(__file__).resolve().parents[2] / "orchestrator" / "rules.json"


def _format(item: dict, kind: str) -> tuple[str, str, str, str]:
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
    original = item.get("source_text")
    source_text = original if isinstance(original, str) and original.strip() else raw
    if kind == "esp32":
        trigger = item.get("trigger") or {}
        action = item.get("action") or {}
        if not isinstance(trigger, dict) or not isinstance(action, dict):
            return str(item.get("name") or "旧规则"), "待检查", "待检查", source_text
        return (str(item.get("name") or "旧规则"),
                f"{trigger.get('tag', '')}：{trigger.get('event', '')}",
                f"{action.get('tag', '')}：{action.get('cmd', '')} {json.dumps(action.get('params', {}), ensure_ascii=False)}", source_text)
    return (str(item.get("name") or item.get("trigger") or "旧教导")[:120],
            str(item.get("trigger") or "待检查"),
            str(item.get("behavior") or item.get("action") or "待检查"), source_text)


def migrate_legacy(*, space_id: str = "main") -> dict[str, int]:
    sources = ((_ESP32_RULES, "esp32_desk_led", "esp32"),
               (RULES_FILE, "go2", "go2"),
               (TAUGHT_FILE, "esp32_desk_led", "lamp"))
    counts = {}
    with _lock:
        for path, unit_id, kind in sources:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError(f"legacy rules are not a list: {path}")
            backup = teachings._path(space_id, unit_id).parents[1] / "_legacy_backups" / f"{kind}-{path.name}.bak"
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(path, backup)
            for index, record in enumerate(data):
                if not isinstance(record, dict):
                    logger.warning("Skipping invalid legacy teaching %s[%d]", path, index)
                    continue
                key = str(path.resolve())
                existing = teachings.list_for(unit_id, space_id=space_id)
                prior = next((item for item in existing
                              if item.get("legacy_file") == key and item.get("legacy_index") == index), None)
                if prior is not None:
                    if not prior.get("legacy_backup"):
                        prior["legacy_backup"] = str(backup.resolve())
                        teachings._save(space_id, unit_id, existing)
                    continue
                name, trigger, behavior, source_text = _format(record, kind)
                status = ("needs_review" if "待检查" in (trigger, behavior) or kind == "esp32"
                          else "remembered")
                item = teachings.remember(
                    unit_id, name[:120], trigger[:500], behavior[:500], source_text[:1000],
                    space_id=space_id, source=f"legacy_{kind}",
                    legacy_id=str(record.get("id") or record.get("rule_id") or ""),
                    legacy_file=key, legacy_backup=str(backup.resolve()),
                    status=status, legacy_index=index)
                counts[kind] = counts.get(kind, 0) + 1
                logger.info("Imported legacy teaching %s[%d] as %s", path, index, item["teaching_id"])
    return counts
