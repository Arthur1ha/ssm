"""Pure map image rendering, run off the API event loop."""


def render_map_png(grid_obj, odom, global_path, locations, target, session_id):
    import io, math as _math
    import numpy as np
    from PIL import Image, ImageDraw

    SCALE = 3

    nx, ny = grid_obj.width[0], grid_obj.width[1]
    W, H   = nx * SCALE, ny * SCALE

    # 翻转行让 Y 轴朝上（北朝上）与世界坐标一致
    from cloud.go2.navigation.occupancy_grid import FREE, OCCUPIED
    arr = np.empty((ny, nx, 3), dtype=np.uint8)
    arr[:] = (34, 65, 70)  # unobserved in-map cells are traversable too
    arr[grid_obj.cells == FREE] = (34, 65, 70)
    arr[grid_obj.cells == OCCUPIED] = (100, 100, 100)  # inflated clearance
    arr[grid_obj.obstacles] = (210, 85, 75)  # raw obstacle returns
    arr = arr[::-1]
    img = Image.fromarray(arr, "RGB").resize((W, H), Image.NEAREST)
    d   = ImageDraw.Draw(img)

    def g2i(ix, iy):
        """grid cell → image pixel，Y 翻转后北朝上"""
        return ix * SCALE + SCALE // 2, (ny - 1 - iy) * SCALE + SCALE // 2

    # 机器狗内置导航规划的全局路径（蓝线）
    if len(global_path) > 1:
        pts = []
        for wp in global_path:
            try:
                gx, gy = grid_obj.odom_to_grid(wp["x"], wp["y"])
                pts.append(g2i(gx, gy))
            except ValueError:
                continue
        if len(pts) > 1:
            d.line(pts, fill=(30, 120, 255), width=2)

    # 保存的地点
    for loc in locations:
        if loc.get("session_id") != session_id:
            continue
        try:
            lx, ly = grid_obj.odom_to_grid(loc["x"], loc["y"])
        except ValueError:
            continue
        px, py = g2i(lx, ly)
        r = 5
        color  = (255, 200, 0) if target == loc["name"] else (160, 200, 80)
        d.ellipse([px-r, py-r, px+r, py+r], fill=color)
        d.text((px + r + 2, py - 5), loc["name"], fill=color)

    # 机器人（绿圆 + 朝向箭头）
    if odom:
        try:
            rix, riy = grid_obj.odom_to_grid(odom["x"], odom["y"])
        except ValueError:
            odom = {}
        else:
            px, py = g2i(rix, riy)
            r = 7
            d.ellipse([px-r, py-r, px+r, py+r], fill=(0, 220, 100), outline=(0, 255, 80), width=2)
            heading = odom["heading"]
            L = 14
            ax = px + int(_math.cos(heading) * L)
            ay = py - int(_math.sin(heading) * L)
            d.line([px, py, ax, ay], fill=(0, 255, 80), width=3)
            d.rectangle([4, 4, 126, 21], fill=(14, 18, 28))
            d.text((7, 7), f"x={odom['x']:.2f} y={odom['y']:.2f}",
                   fill=(190, 245, 205))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
