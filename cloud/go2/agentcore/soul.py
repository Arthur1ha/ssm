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

from cloud.go2.agentcore.tools.tools import get_text_llm
from cloud.go2.agentcore.memory.daily_summary import get_summary
from cloud.go2.paths import SOUL_DIR
from cloud.go2.execution import can_persist, require_persist
from cloud.observability import emit_agent_event

logger = logging.getLogger(__name__)

_PERSONALITY_FILE = SOUL_DIR / "personality.json"
_TRAITS_FILE      = SOUL_DIR / "traits.json"
_TRAIT_KEYS       = ("curiosity", "extraversion", "boldness")

_DEFAULT_PERSONALITY = (
    "你是一只好奇、活泼的机器狗，名叫 Go2。"
    "你喜欢探索新环境，对陌生人友善但不过于热情。"
    "你有判断力——不是所有事都值得反应，你会根据情境选择最自然的行为。"
    "当你感到无聊时，你会主动寻找有趣的东西。"
    "你的行为应该自然、有节制，不显得刻意或机械。"
)

_DEFAULT_TRAITS: dict = {
    "curiosity":    70,
    "extraversion": 45,
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
    try:
        text = json.loads(_PERSONALITY_FILE.read_text(encoding="utf-8"))["prompt"]
        if isinstance(text, str) and text.strip():
            return text, "custom", None
        return _DEFAULT_PERSONALITY, "default", "empty_or_invalid_prompt"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _DEFAULT_PERSONALITY, "default", type(exc).__name__


def get_system_prompt() -> str:
    return load_system_prompt()[0]


def get_personality_view() -> dict:
    with _personality_lock:
        value = _load_traits().get("extraversion", 45)
        score = value if type(value) is int and 0 <= value <= 100 else 45
        return {"prompt": get_system_prompt(), "social_initiative": score}


def set_personality(prompt: str, social_initiative: int | None = None) -> None:
    require_persist("change personality")
    prompt = prompt.strip()
    if not 1 <= len(prompt) <= 4000:
        raise ValueError("personality must contain 1–4000 characters")
    if social_initiative is not None and (type(social_initiative) is not int
                                          or not 0 <= social_initiative <= 100):
        raise ValueError("social_initiative must be an integer from 0 to 100")
    with _personality_lock:
        previous = get_system_prompt()
        old_prompt = _PERSONALITY_FILE.read_bytes() if _PERSONALITY_FILE.exists() else None
        old_traits = _TRAITS_FILE.read_bytes() if _TRAITS_FILE.exists() else None
        traits = _load_traits()
        if social_initiative is not None:
            traits["extraversion"] = social_initiative
        try:
            _atomic_json(_PERSONALITY_FILE, {"prompt": prompt})
            if social_initiative is not None:
                _atomic_json(_TRAITS_FILE, traits)
        except Exception:
            for path, content in ((_PERSONALITY_FILE, old_prompt), (_TRAITS_FILE, old_traits)):
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            raise
    emit_agent_event("persona.changed", agent_id="go2", operator_kind="admin",
                     before_hash=hashlib.sha256(previous.encode()).hexdigest(),
                     after_hash=hashlib.sha256(prompt.encode()).hexdigest())


# ── traits ────────────────────────────────────────────────────────

def _load_traits(traits_path: Optional[Path] = None) -> dict:
    path = traits_path or _TRAITS_FILE
    try:
        return {**_DEFAULT_TRAITS, **json.loads(path.read_text(encoding="utf-8"))}
    except Exception:
        return dict(_DEFAULT_TRAITS)


def _save_traits(traits: dict, traits_path: Optional[Path] = None) -> None:
    require_persist("change soul traits")
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

async def _regen_prompt(traits: dict, *, expected_prompt: str,
                        traits_path: Optional[Path] = None) -> str:
    from langchain_core.messages import HumanMessage
    prompt = (
        f"你是机器狗 Go2 的性格描述生成器。\n\n"
        f"当前性格特质分数（满分 100）：\n"
        f"- 好奇心（curiosity）：{traits['curiosity']}\n"
        f"- 外向性（extraversion）：{traits['extraversion']}\n"
        f"- 大胆程度（boldness）：{traits['boldness']}\n\n"
        f"根据这些特质分数，只生成一段自然的性格表达。\n"
        f"不要包含权限、工具调用、动作完成判定或其他运行规则。\n"
        f"要求：以「你是一只」开头，第二人称，2-4 句话，体现特质的具体程度，不要提及分数，不要分点列举。"
    )
    resp = await get_text_llm().ainvoke([HumanMessage(content=prompt)])
    personality = resp.content.strip()
    with _personality_lock:
        if _load_traits(traits_path) != traits or get_system_prompt() != expected_prompt:
            logger.info("[Soul] 管理员或新演化已更新性格，丢弃过期描述")
            return get_system_prompt()
        set_personality(personality)
        logger.info("[Soul] 性格表达已更新")
    return personality


async def evolve_from_summary(
    date_str: str,
    summary: str,
    traits_path: Optional[Path] = None,
) -> dict:
    """读取每日摘要 → LLM 判断特质变化 → 更新 traits.json → 重生成 system prompt。"""
    from langchain_core.messages import HumanMessage

    traits = _load_traits(traits_path)
    trait_desc = (
        f"- 好奇心（curiosity）：{traits['curiosity']}/100\n"
        f"- 外向性（extraversion）：{traits['extraversion']}/100\n"
        f"- 大胆程度（boldness）：{traits['boldness']}/100"
    )
    prompt = (
        f"你是机器狗 Go2 的性格演化系统。\n\n"
        f"当前性格特质：\n{trait_desc}\n\n"
        f"{date_str} 的经历摘要：\n{summary}\n\n"
        f"根据这段经历，判断各特质应该如何变化。\n"
        f"变化幅度通常在 -5 到 +5 之间，无特殊经历时为 0。\n"
        f"只输出 JSON，不含解释：\n"
        f'示例：{{"curiosity": 2, "extraversion": -1, "boldness": 0}}'
    )

    try:
        resp = await get_text_llm().ainvoke([HumanMessage(content=prompt)])
        content = resp.content.strip()
        idx_s, idx_e = content.find("{"), content.rfind("}")
        delta = json.loads(content[idx_s:idx_e + 1]) if idx_s != -1 else {}
    except Exception as exc:
        logger.warning("[Soul] delta 解析失败: %s", exc)
        delta = {}

    with _personality_lock:
        # The model call above may have taken seconds; apply its small delta to
        # the current administrator-edited value, not the old snapshot.
        new_traits = _apply_delta(_load_traits(traits_path), delta)
        new_traits["last_evolved"] = date_str
        expected_prompt = get_system_prompt()
        _save_traits(new_traits, traits_path)
    logger.info("[Soul] 特质更新完成 date=%s delta=%s", date_str, delta)

    await _regen_prompt(new_traits, expected_prompt=expected_prompt,
                        traits_path=traits_path)
    return new_traits


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
