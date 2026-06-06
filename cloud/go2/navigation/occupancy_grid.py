import numpy as np

# Native 解码器返回米制坐标（世界系）。地板 ≈ z=0，用米制阈值过滤更可靠。
Z_FLOOR_M  = 0.15   # m，低于此 = 地板/低矮地物，忽略
Z_CEIL_M   = 1.50   # m，高于此 = 天花板，忽略
INFLATE_RADIUS   = 3  # cells，障碍膨胀半径（~0.15m）
CLEARANCE_RADIUS = 5  # cells，机器人脚印清除半径（~0.25m，覆盖 Go2 体宽）


class OccupancyGrid:
    def __init__(self, voxel_msg: dict) -> None:
        d = voxel_msg.get("data", {})
        self.resolution: float = d.get("resolution", 0.05)
        self.origin: list[float] = d.get("origin", [0.0, 0.0, 0.0])
        self.width: list[int] = d.get("width", [128, 128, 38])
        # Native 解码器：data.data.points → ndarray(N,3)，单位米，世界系
        self.grid: np.ndarray = self._build(d.get("data", {}).get("points"))

    def _build(self, points) -> np.ndarray:
        nx, ny = self.width[0], self.width[1]
        raw = np.zeros((ny, nx), dtype=bool)
        if points is None:
            return raw
        pts = np.asarray(points)
        if pts.ndim != 2 or pts.shape[1] < 3 or len(pts) == 0:
            return raw
        # Z 过滤：只保留地板以上、天花板以下的点
        mask = (pts[:, 2] > Z_FLOOR_M) & (pts[:, 2] < Z_CEIL_M)
        pts = pts[mask]
        if len(pts) == 0:
            return raw
        # 米制坐标 → 格索引
        ix = np.clip(((pts[:, 0] - self.origin[0]) / self.resolution).astype(int), 0, nx - 1)
        iy = np.clip(((pts[:, 1] - self.origin[1]) / self.resolution).astype(int), 0, ny - 1)
        raw[iy, ix] = True
        inflated = self._inflate(raw)
        self._clear_robot_footprint(inflated, nx, ny)
        return inflated

    def _clear_robot_footprint(self, grid: np.ndarray, nx: int, ny: int) -> None:
        cx, cy = nx // 2, ny // 2
        r = CLEARANCE_RADIUS
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy <= r * r:
                    y_idx, x_idx = cy + dy, cx + dx
                    if 0 <= y_idx < ny and 0 <= x_idx < nx:
                        grid[y_idx, x_idx] = False

    def _inflate(self, grid: np.ndarray) -> np.ndarray:
        if not grid.any():
            return grid
        result = grid.copy()
        r = INFLATE_RADIUS
        ys, xs = np.where(grid)
        ny, nx = grid.shape
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy <= r * r:
                    ny2 = np.clip(ys + dy, 0, ny - 1)
                    nx2 = np.clip(xs + dx, 0, nx - 1)
                    result[ny2, nx2] = True
        return result

    def odom_to_grid(self, x: float, y: float) -> tuple[int, int]:
        ix = int((x - self.origin[0]) / self.resolution)
        iy = int((y - self.origin[1]) / self.resolution)
        ix = max(0, min(ix, self.width[0] - 1))
        iy = max(0, min(iy, self.width[1] - 1))
        return ix, iy

    def grid_to_odom(self, ix: int, iy: int) -> tuple[float, float]:
        x = self.origin[0] + (ix + 0.5) * self.resolution
        y = self.origin[1] + (iy + 0.5) * self.resolution
        return x, y

    def is_free(self, ix: int, iy: int) -> bool:
        if ix < 0 or iy < 0 or ix >= self.width[0] or iy >= self.width[1]:
            return False
        return not self.grid[iy, ix]
