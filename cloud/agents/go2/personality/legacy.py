"""soul.py — 性格存储（personality）与特质演化（soul evolution）。"""
import json
import hashlib
import logging
import os
import tempfile
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from cloud.go2.tools.tools import get_text_llm
from cloud.agents.go2.memory.daily_summary import get_summary
from cloud.go2.paths import SOUL_DIR
from cloud.go2.execution import can_persist, require_persist
from cloud.observability import emit_agent_event
from cloud.agents.go2.personality import profile as unified_profile

logger = logging.getLogger(__name__)

_PERSONALITY_FILE = SOUL_DIR / "personality.json"
_TRAITS_FILE      = SOUL_DIR / "traits.json"
_TRAIT_KEYS       = ("curiosity", "extraversion", "boldness")

_DEFAULT_PERSONALITY = unified_profile.DEFAULT_PROMPT

_DEFAULT_TRAITS: dict = {
    "curiosity":    70,
    "extraversion": 80,
    "boldness":     60,
    "last_evolved": None,
}
_personality_lock = threading.RLock()


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".soul-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# ── personality ───────────────────────────────────────────────────

def load_system_prompt() -> tuple[str, str, str | None]:
    """Return the mutable personality expression, not runtime safety rules."""
    prompt = unified_profile.get_profile()["prompt"]
    return prompt, "default" if prompt == _DEFAULT_PERSONALITY else "custom", None


def get_system_prompt() -> str:
    return load_system_prompt()[0]


def get_personality_view() -> dict:
    current = unified_profile.get_profile()
    return {"prompt": current["prompt"],
            "social_initiative": current["traits"]["extraversion"]}


def set_personality(prompt: str, social_initiative: int | None = None) -> None:
    require_persist("change personality")
    previous = get_system_prompt()
    current = unified_profile.get_profile()
    changes = {"prompt": prompt, "revision": current["revision"]}
    if social_initiative is not None:
        changes["traits"] = {"extraversion": social_initiative}
    unified_profile.update_profile(changes, {"actor": "admin", "persist": True})
    emit_agent_event("persona.changed", agent_id="go2", operator_kind="admin",
                     before_hash=hashlib.sha256(previous.encode()).hexdigest(),
                     after_hash=hashlib.sha256(prompt.encode()).hexdigest())


# ── traits ────────────────────────────────────────────────────────

def _load_traits(traits_path: Optional[Path] = None) -> dict:
    if traits_path is None:
        current = unified_profile.get_profile()
        return {**current["traits"], **unified_profile.get_evolution_metadata()}
    path = traits_path or _TRAITS_FILE
    try:
        return {**_DEFAULT_TRAITS, **json.loads(path.read_text(encoding="utf-8"))}
    except Exception:
        return dict(_DEFAULT_TRAITS)


def _save_traits(traits: dict, traits_path: Optional[Path] = None) -> None:
    require_persist("change soul traits")
    if traits_path is None:
        current = unified_profile.get_profile()
        changes = {"traits": {key: traits[key] for key in _TRAIT_KEYS if key in traits},
                   "revision": current["revision"]}
        unified_profile.update_profile(changes, {"actor": "admin", "persist": True})
        return
    path = traits_path or _TRAITS_FILE
    with _personality_lock:
        _atomic_json(path, traits)


def _apply_delta(traits: dict, delta: dict) -> dict:
    updated = dict(traits)
    for key in _TRAIT_KEYS:
        if key in delta:
            updated[key] = max(0, min(100, traits[key] + int(delta[key])))
    return updated


# ── evolution ─────────────────────────────────────────────────────

async def evolve_from_summary(
    date_str: str,
    summary: str,
    traits_path: Optional[Path] = None,
) -> dict:
    """Evolve Go2 traits through the checkpointed role Graph."""
    from cloud.agents.go2.workflows.evolution import run_evolution_graph
    if traits_path is None:
        result = await run_evolution_graph(date_str, summary, model=get_text_llm())
        return {**result.get("traits", unified_profile.get_profile()["traits"]),
                "last_evolved": unified_profile.get_evolution_metadata().get("last_evolved")}

    def load_profile():
        return {"traits": _load_traits(traits_path)}

    def load_metadata():
        return {"last_evolved": _load_traits(traits_path).get("last_evolved")}

    def commit(delta: dict, source_date: str):
        with _personality_lock:
            updated = _apply_delta(_load_traits(traits_path), delta)
            updated["last_evolved"] = source_date
            _save_traits(updated, traits_path)
            return {"traits": updated}

    result = await run_evolution_graph(
        date_str, summary, model=get_text_llm(), profile_loader=load_profile,
        metadata_loader=load_metadata, commit=commit,
        checkpoint_scope=str(traits_path.resolve()))
    return result.get("traits", _load_traits(traits_path))


async def ensure_yesterday_evolved(traits_path: Optional[Path] = None) -> None:
    """懒触发：若昨天有日摘要但尚未演化，则执行演化。"""
    if not can_persist():
        return
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    traits = _load_traits(traits_path)
    if (traits.get("last_evolved") or "") >= yesterday:
        return

    summary = get_summary(yesterday)
    if not summary:
        return

    logger.info("[Soul] 昨天（%s）缺少性格演化，后台生成中...", yesterday)
    await evolve_from_summary(yesterday, summary, traits_path=traits_path)
