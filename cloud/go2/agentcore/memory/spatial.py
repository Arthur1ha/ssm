"""空间记忆：命名地点的读写（单个 spatial.json）。"""
import json
import logging
import time
from typing import Optional

from cloud.go2.paths import MEMORY_DIR

logger = logging.getLogger(__name__)

SPATIAL_FILE = MEMORY_DIR / "spatial.json"


def _load() -> dict:
    """读取地点字典，文件缺失或损坏时返回空字典。"""
    try:
        return json.loads(SPATIAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    """全量回写地点字典。"""
    SPATIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SPATIAL_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def tag_location(name: str, odom: dict) -> str:
    """把当前位置保存为命名地点，同名覆盖。"""
    data = _load()
    data[name] = {
        "x":           odom["x"],
        "y":           odom["y"],
        "heading":     odom["heading"],
        "description": "",
        "ts":          time.time(),
    }
    _save(data)
    logger.info("[SpatialMemory] 保存地点「%s」(%.2f, %.2f)", name, odom["x"], odom["y"])
    return f"已保存地点「{name}」({odom['x']:.2f}, {odom['y']:.2f})"


async def find_location(query: str) -> Optional[dict]:
    """精确按名取地点，失败则交给 LLM 模糊匹配。"""
    data = _load()
    if query in data:
        v = data[query]
        return {"name": query, "x": v["x"], "y": v["y"], "heading": v["heading"]}
    return await _llm_find(query)


async def _llm_find(query: str) -> Optional[dict]:
    from langchain_core.messages import HumanMessage
    names = list(_load().keys())
    if not names:
        return None
    logger.info("[SpatialMemory] 精确匹配失败，LLM 模糊查找「%s」，候选：%s", query, names)
    prompt = (
        f"用户想去的地点：「{query}」\n"
        f"已知地点列表：{names}\n"
        f"从列表中选出最匹配的地点名，只输出地点名，无法匹配则输出 null"
    )
    resp = await _get_text_llm().ainvoke([HumanMessage(content=prompt)])
    matched = resp.content.strip().strip('"').strip("'")
    if matched == "null" or matched not in names:
        logger.info("[SpatialMemory] LLM 未找到匹配地点")
        return None
    logger.info("[SpatialMemory] LLM 匹配结果：「%s」", matched)
    return await find_location(matched)


def _get_text_llm():
    from cloud.go2.agentcore.tools.tools import get_text_llm
    return get_text_llm()


def list_locations() -> list[dict]:
    """列出所有命名地点，按保存时间倒序。"""
    data = _load()
    items = [
        {"name": name, "x": v["x"], "y": v["y"], "heading": v["heading"], "ts": v["ts"]}
        for name, v in data.items()
    ]
    items.sort(key=lambda r: r["ts"], reverse=True)
    return items


def delete_location(name: str) -> bool:
    """删除命名地点，存在则返回 True。"""
    data = _load()
    if name in data:
        del data[name]
        _save(data)
        return True
    return False
