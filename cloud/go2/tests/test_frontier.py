# cloud/go2/tests/test_frontier.py
"""frontier.py 单元测试：射线边界停止 + 探索目标范围约束。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import math
import numpy as np
import pytest

from cloud.go2.navigation.occupancy_grid import OccupancyGrid
from cloud.go2.navigation import frontier as frontier_mod
from cloud.go2.navigation.frontier import _raycast, find_exploration_target


def _make_grid(resolution=0.1, origin=None, nx=10, ny=10, points=None):
    """构造测试用栅格，默认 10×10 格、0.1m 分辨率（= 1m×1m）。"""
    if origin is None:
        origin = [-0.5, -0.5, 0.0]
    data_inner = {}
    if points is not None:
        data_inner["points"] = np.array(points, dtype=float)
    return OccupancyGrid({
        "data": {
            "resolution": resolution,
            "origin": origin,
            "width": [nx, ny, 1],
            "data": data_inner,
        }
    })


# ── _raycast 边界停止 ──────────────────────────────────────────────────────


def test_raycast_stops_at_grid_boundary_positive_x():
    """Bug A 修复①：射线向 +x 投射，应在地图边界处停止，不穿透盲区返回 MAX_DIST。"""
    # 栅格 10×10、0.1m/cell（1m 见方），旧代码穿透边界返回 _MAX_RAYCAST_DIST（3.0 或 4.0）
    grid = _make_grid()
    dist = _raycast(grid, 0.0, 0.0, 0.0)  # angle=0 → +x
    assert dist < frontier_mod._MAX_RAYCAST_DIST, (
        f"射线应在地图边界（<1m）处停止，不应返回 _MAX_RAYCAST_DIST={frontier_mod._MAX_RAYCAST_DIST}m，"
        f"实际返回 {dist:.2f}m"
    )
    assert dist <= 1.0, (
        f"射线不应超过栅格总宽度 1.0m，实际返回 {dist:.2f}m"
    )


def test_raycast_stops_at_grid_boundary_negative_x():
    """射线向 -x 投射，同样在地图西侧边界停止，不超过栅格宽度。"""
    grid = _make_grid()
    dist = _raycast(grid, 0.0, 0.0, math.pi)  # angle=π → -x
    assert dist < frontier_mod._MAX_RAYCAST_DIST, (
        f"射线应在地图边界处停止，不应返回 {frontier_mod._MAX_RAYCAST_DIST}m，实际 {dist:.2f}m"
    )
    assert dist <= 1.0, (
        f"射线不应超过栅格总宽度 1.0m，实际返回 {dist:.2f}m"
    )


def test_raycast_returns_obstacle_distance_before_boundary():
    """有障碍时，射线应在障碍处停止（短于边界距离）。"""
    # 使用 40×40 栅格避免 CLEARANCE_RADIUS=5 清除目标格
    # 障碍在 x=1.5m（距机器人 1.5m），边界在 x=2.0m，应在障碍前停止
    grid = _make_grid(nx=40, ny=40, origin=[-2.0, -2.0, 0.0],
                      points=[[1.5, 0.0, 0.5]])  # z=0.5m 在有效范围内
    dist = _raycast(grid, 0.0, 0.0, 0.0)  # → +x
    assert dist < 1.5, (
        f"射线应在障碍（~1.5m）前停止，实际返回 {dist:.2f}m"
    )
    assert dist < frontier_mod._MAX_RAYCAST_DIST


def test_raycast_max_dist_honored_within_grid():
    """在足够大的栅格内，射线应不超过 _MAX_RAYCAST_DIST。"""
    # 栅格 40×40 格，0.1m，= 4m×4m，大于 _MAX_RAYCAST_DIST=3.0m
    grid = _make_grid(nx=40, ny=40, origin=[-2.0, -2.0, 0.0])
    dist = _raycast(grid, 0.0, 0.0, 0.0)
    assert dist <= frontier_mod._MAX_RAYCAST_DIST, (
        f"射线不应超过 _MAX_RAYCAST_DIST={frontier_mod._MAX_RAYCAST_DIST}m，实际 {dist:.2f}m"
    )


# ── find_exploration_target 范围约束 ──────────────────────────────────────


def test_find_exploration_target_within_grid_bounds():
    """Bug A 修复①：find_exploration_target 返回的目标不超出栅格覆盖范围。"""
    # 栅格 40×40、0.05m，覆盖 x∈[-1,1]、y∈[-1,1]
    grid = _make_grid(resolution=0.05, nx=40, ny=40, origin=[-1.0, -1.0, 0.0])
    odom = {"x": 0.0, "y": 0.0, "heading": 0.0}
    target = find_exploration_target(grid, odom)
    if target is not None:
        tx, ty = target
        # 栅格边界（留一格安全余量）
        assert -1.0 <= tx <= 1.0, f"目标 x={tx:.2f} 超出栅格范围 [-1,1]"
        assert -1.0 <= ty <= 1.0, f"目标 y={ty:.2f} 超出栅格范围 [-1,1]"


def test_find_exploration_target_preferred_direction():
    """preferred_direction 受边界约束，不应返回盲区目标。"""
    grid = _make_grid(nx=40, ny=40, origin=[-2.0, -2.0, 0.0])
    odom = {"x": 0.0, "y": 0.0, "heading": 0.0}
    target = find_exploration_target(grid, odom, preferred_direction="forward")
    if target is not None:
        tx, ty = target
        assert -2.0 <= tx <= 2.0
        assert -2.0 <= ty <= 2.0


def test_raycast_directions_all_bounded():
    """raycast_directions 8 个方向的结果均 ≤ _MAX_RAYCAST_DIST。"""
    grid = _make_grid(nx=40, ny=40, origin=[-2.0, -2.0, 0.0])
    odom = {"x": 0.0, "y": 0.0, "heading": 0.0}
    result = frontier_mod.raycast_directions(grid, odom)
    for direction, dist in result.items():
        assert dist <= frontier_mod._MAX_RAYCAST_DIST, (
            f"方向 {direction} 距离 {dist:.2f}m 超过 _MAX_RAYCAST_DIST={frontier_mod._MAX_RAYCAST_DIST}m"
        )
