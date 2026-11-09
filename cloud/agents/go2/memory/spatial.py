"""空间记忆：命名地点的读写（单个 spatial.json）。"""
import json
import logging
import time
from typing import Optional

from cloud.go2.paths import MEMORY_DIR
from cloud.space.execution import require_persist
from cloud.go2.models import get_text_llm

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
    require_persist("modify spatial memory")
    SPATIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SPATIAL_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def tag_location(name: str, odom: dict) -> str:
    """把当前位置保存为命名地点，同名覆盖。"""
    require_persist("save a location")
    if not odom.get("session_id"):
        raise ValueError("当前位置没有有效 odom 会话，不能保存地点")
    data = _load()
    data[name] = {
        "x":           odom["x"],
        "y":           odom["y"],
        "heading":     odom["heading"],
        "frame_id":    "odom",
        "session_id":  odom["session_id"],
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
        return {"name": query, "x": v["x"], "y": v["y"], "heading": v["heading"],
                "frame_id": v["frame_id"], "session_id": v["session_id"]}
    return await _llm_find(query)


async def _llm_find(query: str) -> Optional[dict]:
    from cloud.agents.go2.workflows.spatial_lookup import run_spatial_lookup
    names = list(_load().keys())
    if not names:
        return None
    logger.info("[SpatialMemory] 精确匹配失败，LLM 模糊查找「%s」，候选：%s", query, names)
    matched = await run_spatial_lookup(query, names, get_text_llm())
    if matched is None:
        logger.info("[SpatialMemory] LLM 未找到匹配地点")
        return None
    logger.info("[SpatialMemory] LLM 匹配结果：「%s」", matched)
    return await find_location(matched)


def list_locations() -> list[dict]:
    """列出所有命名地点，按保存时间倒序。"""
    data = _load()
    items = [
        {"name": name, "x": v["x"], "y": v["y"], "heading": v["heading"], "ts": v["ts"],
         "frame_id": v["frame_id"], "session_id": v["session_id"]}
        for name, v in data.items()
    ]
    items.sort(key=lambda r: r["ts"], reverse=True)
    return items


def delete_location(name: str) -> bool:
    """删除命名地点，存在则返回 True。"""
    require_persist("delete a location")
    data = _load()
    if name in data:
        del data[name]
        _save(data)
        return True
    return False
