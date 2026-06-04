import numpy as np

Z_MIN_IDX = 5   # iz < this = floor, ignored
Z_MAX_IDX = 36  # iz > this = ceiling, ignored
INFLATE_RADIUS = 3  # obstacle inflation radius in cells (~0.15m)


class OccupancyGrid:
    def __init__(self, voxel_msg: dict) -> None:
        d = voxel_msg.get("data", {})
        self.resolution: float = d.get("resolution", 0.05)
        self.origin: list[float] = d.get("origin", [0.0, 0.0, 0.0])
        self.width: list[int] = d.get("width", [128, 128, 38])
        self.grid: np.ndarray = self._build(d.get("data", {}).get("positions"))

    def _build(self, positions) -> np.ndarray:
        nx, ny = self.width[0], self.width[1]
        raw = np.zeros((ny, nx), dtype=bool)
        if positions is None:
            return raw
        pos = np.asarray(positions).reshape(-1, 3)
        mask = (pos[:, 2] >= Z_MIN_IDX) & (pos[:, 2] <= Z_MAX_IDX)
        pts = pos[mask]
        if len(pts):
            ix = np.clip(pts[:, 0], 0, nx - 1)
            iy = np.clip(pts[:, 1], 0, ny - 1)
            raw[iy, ix] = True
        return self._inflate(raw)

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
