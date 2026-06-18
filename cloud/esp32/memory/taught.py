"""灯智能体调教记忆：主人教的行为习惯，单文件 JSON 持久化。

仿 cloud/go2/agentcore/memory/spatial.py 的读写风格；执行体私有，不跨设备共享。
"""
import json
import logging
import time
from pathlib import Path
from typing import Optional

from cloud.esp32.paths import TAUGHT_FILE

logger = logging.getLogger(__name__)


def _load(path: Optional[Path] = None) -> list:
    """读取调教记录列表，文件缺失或损坏时返回空列表。"""
    p = path or TAUGHT_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(rules: list, path: Optional[Path] = None) -> None:
    """全量原子回写调教记录列表。"""
    p = path or TAUGHT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")


def add(trigger: str, behavior: str, action_hint: Optional[dict] = None,
        cooldown_s: int = 30, source: str = "admin",
        path: Optional[Path] = None) -> dict:
    """新增一条调教记录并落盘，返回该记录。"""
    rules = _load(path)
    rule = {
        "id":            f"tb_{int(time.time() * 1000)}",
        "trigger":       trigger,
        "behavior":      behavior,
        "action_hint":   action_hint or {},
        "cooldown_s":    cooldown_s,
        "source":        source,
        "enabled":       True,
        "created_ts":    time.time(),
        "hit_count":     0,
        "last_fired_ts": None,
    }
    rules.append(rule)
    _save(rules, path)
    logger.info("[Taught/esp32] +调教：%s → %s", trigger, behavior)
    return rule


def list_all(path: Optional[Path] = None) -> list:
    """列出全部调教记录。"""
    return _load(path)


def delete(rule_id: str, path: Optional[Path] = None) -> bool:
    """按 id 删除，存在则返回 True。"""
    rules = _load(path)
    kept = [r for r in rules if r.get("id") != rule_id]
    if len(kept) == len(rules):
        return False
    _save(kept, path)
    return True


def touch(rule_id: str, path: Optional[Path] = None) -> None:
    """命中一次：hit_count+1，刷新 last_fired_ts。"""
    rules = _load(path)
    for r in rules:
        if r.get("id") == rule_id:
            r["hit_count"] = r.get("hit_count", 0) + 1
            r["last_fired_ts"] = time.time()
    _save(rules, path)
