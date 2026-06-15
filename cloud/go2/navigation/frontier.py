"""
射线投射与探索目标生成：给 Drive 探索层提供有意义的移动目标。

Drive 负责"想去哪个方向"，frontier 负责"那个方向能安全走多远"。
"""
import math
from typing import Optional

_MAX_RAYCAST_DIST = 3.0  # m，单条射线最大探测距离（不超出栅格有效半径 ~3.2m）
_TARGET_RATIO     = 0.8  # 取自由距离的 80% 作为目标，留安全余量
_MIN_MOVE_DIST    = 0.5  # m，低于此说明该方向被堵死，不值得导航

# 8 方向相对于机器人当前朝向的偏角（rad）
_DIRECTION_OFFSETS: dict[str, float] = {
    "forward":        0.0,
    "forward_left":   math.pi / 4,
    "left":           math.pi / 2,
    "backward_left":  3 * math.pi / 4,
    "backward":       math.pi,
    "backward_right": -3 * math.pi / 4,
    "right":          -math.pi / 2,
    "forward_right":  -math.pi / 4,
}

DIRECTIONS = list(_DIRECTION_OFFSETS.keys())


def _raycast(grid_obj, x: float, y: float, angle_rad: float) -> float:
    """从 (x, y) 沿 angle_rad 方向射线投射，返回碰到障碍或地图边界前的自由距离（米）。

    使用未截断的原始格索引判断越界，射线到达栅格边界时立即停止，
    避免 odom_to_grid 的截断把盲区误判为可通行。
    """
    step  = grid_obj.resolution
    steps = int(_MAX_RAYCAST_DIST / step)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    nx, ny   = grid_obj.width[0], grid_obj.width[1]
    ox, oy   = grid_obj.origin[0], grid_obj.origin[1]
    for i in range(1, steps + 1):
        wx = x + cos_a * step * i
        wy = y + sin_a * step * i
        ix_raw = int((wx - ox) / step)
        iy_raw = int((wy - oy) / step)
        if not (0 <= ix_raw < nx and 0 <= iy_raw < ny):
            return step * (i - 1)  # 到达地图边界，停止
        if not grid_obj.is_free(ix_raw, iy_raw):
            return step * (i - 1)
    return _MAX_RAYCAST_DIST


def raycast_directions(grid_obj, odom: dict) -> dict[str, float]:
    """对 8 个方向射线投射，返回各方向可行距离（米）。

    方向以机器人当前朝向（heading）为前方基准。
    """
    heading = odom.get("heading", 0.0)
    x, y = odom["x"], odom["y"]
    return {
        direction: round(_raycast(grid_obj, x, y, heading + offset), 2)
        for direction, offset in _DIRECTION_OFFSETS.items()
    }


def find_exploration_target(
    grid_obj,
    odom: dict,
    preferred_direction: Optional[str] = None,
) -> Optional[tuple[float, float]]:
    """生成探索目标坐标（世界系，米）。

    有 preferred_direction 时优先该方向；否则自动选自由空间最大的方向。
    返回 None 表示所有方向空间不足，无需移动。
    """
    heading = odom.get("heading", 0.0)
    x, y = odom["x"], odom["y"]

    if preferred_direction and preferred_direction in _DIRECTION_OFFSETS:
        angle    = heading + _DIRECTION_OFFSETS[preferred_direction]
        free_dist = _raycast(grid_obj, x, y, angle)
    else:
        best     = max(_DIRECTION_OFFSETS, key=lambda d: _raycast(grid_obj, x, y, heading + _DIRECTION_OFFSETS[d]))
        angle    = heading + _DIRECTION_OFFSETS[best]
        free_dist = _raycast(grid_obj, x, y, angle)

    move_dist = free_dist * _TARGET_RATIO
    if move_dist < _MIN_MOVE_DIST:
        return None

    return (
        x + math.cos(angle) * move_dist,
        y + math.sin(angle) * move_dist,
    )
