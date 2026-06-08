"""性格演化：LLM 读取每日摘要 → 更新特质分 → 重生成 system prompt。"""
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_TRAITS_FILE = Path(__file__).parent / "traits.json"
_TRAIT_KEYS = ("curiosity", "extraversion", "boldness")
_DEFAULT_TRAITS: dict = {
    "curiosity": 70,
    "extraversion": 45,
    "boldness": 60,
    "last_evolved": None,
}


def _load_traits(traits_path: Optional[Path] = None) -> dict:
    """读取 traits.json，文件不存在或损坏时返回默认值。"""
    path = traits_path or _TRAITS_FILE
    try:
        return {**_DEFAULT_TRAITS, **json.loads(path.read_text(encoding="utf-8"))}
    except Exception:
        return dict(_DEFAULT_TRAITS)


def _save_traits(traits: dict, traits_path: Optional[Path] = None) -> None:
    """将特质字典写入 traits.json。"""
    path = traits_path or _TRAITS_FILE
    path.write_text(json.dumps(traits, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_delta(traits: dict, delta: dict) -> dict:
    """将 delta 叠加到特质分，结果 clamp 到 [0, 100]，未知键忽略。"""
    updated = dict(traits)
    for key in _TRAIT_KEYS:
        if key in delta:
            updated[key] = max(0, min(100, traits[key] + int(delta[key])))
    return updated
