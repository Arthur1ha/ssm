"""soul.py — 性格存储（personality）与特质演化（soul evolution）。"""
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from cloud.go2.agentcore.tools.tools import get_text_llm
from cloud.go2.agentcore.memory.daily_summary import get_summary

logger = logging.getLogger(__name__)

_PERSONALITY_FILE = Path(__file__).parent / "personality.json"
_TRAITS_FILE      = Path(__file__).parent / "traits.json"
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


# ── personality ───────────────────────────────────────────────────

def get_system_prompt() -> str:
    try:
        return json.loads(_PERSONALITY_FILE.read_text(encoding="utf-8"))["prompt"]
    except Exception:
        return _DEFAULT_PERSONALITY


def set_personality(prompt: str) -> None:
    _PERSONALITY_FILE.write_text(
        json.dumps({"prompt": prompt}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── traits ────────────────────────────────────────────────────────

def _load_traits(traits_path: Optional[Path] = None) -> dict:
    path = traits_path or _TRAITS_FILE
    try:
        return {**_DEFAULT_TRAITS, **json.loads(path.read_text(encoding="utf-8"))}
    except Exception:
        return dict(_DEFAULT_TRAITS)


def _save_traits(traits: dict, traits_path: Optional[Path] = None) -> None:
    path = traits_path or _TRAITS_FILE
    path.write_text(json.dumps(traits, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_delta(traits: dict, delta: dict) -> dict:
    updated = dict(traits)
    for key in _TRAIT_KEYS:
        if key in delta:
            updated[key] = max(0, min(100, traits[key] + int(delta[key])))
    return updated


# ── evolution ─────────────────────────────────────────────────────

async def _regen_prompt(traits: dict) -> str:
    from langchain_core.messages import HumanMessage
    prompt = (
        f"你是机器狗 Go2 的性格描述生成器。\n\n"
        f"当前性格特质分数（满分 100）：\n"
        f"- 好奇心（curiosity）：{traits['curiosity']}\n"
        f"- 外向性（extraversion）：{traits['extraversion']}\n"
        f"- 大胆程度（boldness）：{traits['boldness']}\n\n"
        f"根据这些特质分数，生成一段自然的性格描述，作为 system prompt 使用。\n"
        f"要求：以「你是一只」开头，第二人称，2-4 句话，体现特质的具体程度，不要提及分数，不要分点列举。"
    )
    resp = await get_text_llm().ainvoke([HumanMessage(content=prompt)])
    personality = resp.content.strip()
    set_personality(personality)
    logger.info("[Soul] system prompt 已更新")
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

    new_traits = _apply_delta(traits, delta)
    new_traits["last_evolved"] = date_str
    _save_traits(new_traits, traits_path)
    logger.info("[Soul] 特质更新完成 date=%s delta=%s", date_str, delta)

    await _regen_prompt(new_traits)
    return new_traits


async def ensure_yesterday_evolved(traits_path: Optional[Path] = None) -> None:
    """懒触发：若昨天有日摘要但尚未演化，则执行演化。"""
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    traits = _load_traits(traits_path)
    if (traits.get("last_evolved") or "") >= yesterday:
        return

    summary = get_summary(yesterday)
    if not summary:
        return

    logger.info("[Soul] 昨天（%s）缺少性格演化，后台生成中...", yesterday)
    await evolve_from_summary(yesterday, summary, traits_path=traits_path)
