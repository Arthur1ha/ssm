import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MEMORY_DB = Path(__file__).parent / "spatial.db"


def _get_conn() -> sqlite3.Connection:
    c = sqlite3.connect(MEMORY_DB)
    c.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            name        TEXT NOT NULL,
            x           REAL NOT NULL,
            y           REAL NOT NULL,
            heading     REAL NOT NULL,
            description TEXT DEFAULT '',
            ts          REAL NOT NULL,
            is_named    INTEGER DEFAULT 0
        )
    """)
    c.commit()
    return c


def tag_location(name: str, odom: dict) -> str:
    with _get_conn() as c:
        c.execute("DELETE FROM locations WHERE name = ? AND is_named = 1", (name,))
        c.execute(
            "INSERT INTO locations (name, x, y, heading, description, ts, is_named) "
            "VALUES (?, ?, ?, ?, '', ?, 1)",
            (name, odom["x"], odom["y"], odom["heading"], time.time()),
        )
    logger.info("[SpatialMemory] 保存地点「%s」(%.2f, %.2f)", name, odom["x"], odom["y"])
    return f"已保存地点「{name}」({odom['x']:.2f}, {odom['y']:.2f})"


async def find_location(query: str) -> Optional[dict]:
    with _get_conn() as c:
        row = c.execute(
            "SELECT name, x, y, heading FROM locations WHERE name = ? AND is_named = 1",
            (query,),
        ).fetchone()
    if row:
        return {"name": row[0], "x": row[1], "y": row[2], "heading": row[3]}
    return await _llm_find(query)


async def _llm_find(query: str) -> Optional[dict]:
    from langchain_core.messages import HumanMessage
    with _get_conn() as c:
        rows = c.execute(
            "SELECT name FROM locations WHERE is_named = 1"
        ).fetchall()
    if not rows:
        return None
    names = [r[0] for r in rows]
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


def record_trajectory_tick(odom: dict, description: str = "") -> None:
    with _get_conn() as c:
        c.execute(
            "INSERT INTO locations (name, x, y, heading, description, ts, is_named) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (f"_traj_{int(time.time() * 1000)}", odom["x"], odom["y"],
             odom["heading"], description, time.time()),
        )


def list_locations() -> list[dict]:
    with _get_conn() as c:
        rows = c.execute(
            "SELECT name, x, y, heading, ts FROM locations "
            "WHERE is_named = 1 ORDER BY ts DESC"
        ).fetchall()
    return [
        {"name": r[0], "x": r[1], "y": r[2], "heading": r[3], "ts": r[4]}
        for r in rows
    ]


def delete_location(name: str) -> bool:
    with _get_conn() as c:
        affected = c.execute(
            "DELETE FROM locations WHERE name = ? AND is_named = 1", (name,)
        ).rowcount
    return affected > 0
