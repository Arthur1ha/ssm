"""性格演化：LLM 读取每日摘要 → 更新特质分 → 重生成 system prompt。"""
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from cloud.go2.agentcore.tools.tools import get_text_llm

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


async def _regen_prompt(traits: dict) -> str:
    """根据特质分调用 LLM 生成自然语言 system prompt，保存到 personality.json。"""
    from langchain_core.messages import HumanMessage
    from cloud.go2.agentcore.soul import set_personality

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
    logger.info("[SoulEvolution] system prompt 已更新")
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
        logger.warning("[SoulEvolution] delta 解析失败: %s", exc)
        delta = {}

    new_traits = _apply_delta(traits, delta)
    new_traits["last_evolved"] = date_str
    _save_traits(new_traits, traits_path)
    logger.info("[SoulEvolution] 特质更新完成 date=%s delta=%s", date_str, delta)

    await _regen_prompt(new_traits)
    return new_traits
