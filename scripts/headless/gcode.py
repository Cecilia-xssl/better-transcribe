"""笔画（页面 mm）→ GRBL G-code。

笔控模型（与 default.properties 对齐，可配置）：
- Stepper Z：抬笔 G0 Z{zOff}，落笔 G0 Z{zOn}
- 或舵机：M280 P0 S{penUp/penDown}（备用）
"""

from __future__ import annotations

from dataclasses import dataclass

from layout import Stroke

MAX_CONTACT_MM = 15.0  # 连续落笔上限：超过即抬笔微移再续（长线/长曲线均适用，防纸面扭曲）


@dataclass
class GCodeConfig:
    feed: float = 4800.0        # 画线速度 mm/min（机器上限 $110=5000）
    travel: float = 5000.0      # 空程速度（机器上限；左移自动 85%）
    z_on: float = 5.0           # 落笔 Z（mm）
    z_off: float = 0.0          # 抬笔 Z
    z_speed: float = 3500.0     # Z 移动速度（过快会抬笔不到位拖笔）
    z_down_speed: float = 1500.0  # 落笔速度（慢落笔控深，防首冲过浅）
    use_z: bool = True
    pen_up_cmd: str = ""        # 非 Z 模式时的抬笔指令（如 M280 P0 S50）
    pen_down_cmd: str = ""
    origin: tuple[float, float] = (0.0, 0.0)  # 纸张左上角在机器坐标的位置
    flip_x: bool = False        # 实测本机：纸坐标=机器坐标 1:1（原点左上，+X 右，+Y 下）
    flip_y: bool = False
    machine_w: float = 210.0
    machine_h: float = 297.0
    park_x: float = 180.0       # 收尾停靠点（机器坐标，可经模板 park_x/park_y 自定义）
    park_y: float = -3.0
    corner_x: float = 150.0     # 换纸停靠点（右下角）：每页写完停这里，松电机等推回
    corner_y: float = 225.0
    tilt_x: float = 0.0         # 立体补偿：右缘落笔修正 mm（+ 加深 / − 变浅），基准左上角
    tilt_y: float = 0.0         # 立体补偿：下缘落笔修正 mm（+ 加深 / − 变浅）
    dot_x: float | None = None  # 触纸预热点（纸上定位轻点位）；None=退化为空中预热
    dot_y: float | None = None


def to_machine(x: float, y: float, cfg: GCodeConfig) -> tuple[float, float]:
    mx = cfg.origin[0] + (cfg.machine_w - x if cfg.flip_x else x)
    my = cfg.origin[1] + (cfg.machine_h - y if cfg.flip_y else y)
    return mx, my


def safe_lift_z(cfg: GCodeConfig) -> float:
    """全平面安全抬笔 Z（本机 +Z=落笔方向）。

    抬笔若按位置补偿，从补偿大的一侧（触纸深）平移到浅侧时会低于浅侧触纸面
    拖出轨迹线（补偿差 ≥ 抬笔行程即触纸）。取补偿最小值（全平面触纸最浅面）
    统一抬笔：任意方向平移净空恒 = 抬笔行程 + 补偿差 ≥ 抬笔行程，双向安全。
    """
    comp_min = min(0.0, cfg.tilt_x, cfg.tilt_y, cfg.tilt_x + cfg.tilt_y)
    return cfg.z_off + comp_min


def strokes_to_gcode(strokes: list[Stroke], cfg: GCodeConfig | None = None,
                     final: bool = True) -> list[str]:
    """final=False：页间段落——收尾停右下角 + 松电机 + 换纸标记（不加 M2）。"""
    cfg = cfg or GCodeConfig()
    out: list[str] = []
    out.append("G21")           # mm
    out.append("G90")           # 绝对坐标

    # 立体补偿基准：纸区在机器坐标的范围（左上→右下归一 0..1）
    _p0 = to_machine(0.0, 0.0, cfg)
    _p1 = to_machine(cfg.machine_w, cfg.machine_h, cfg)
    _xs = sorted((_p0[0], _p1[0]))
    _ys = sorted((_p0[1], _p1[1]))
    cur_x, cur_y = cfg.park_x, cfg.park_y  # 当前机器位置（Z 补偿采样点）

    def z_comp(mx: float, my: float) -> float:
        """平面倾斜补偿：tilt_x=右缘修正、tilt_y=下缘修正（+ = 该侧落笔加深）。"""
        if not (cfg.tilt_x or cfg.tilt_y):
            return 0.0
        u = (mx - _xs[0]) / (_xs[1] - _xs[0]) if _xs[1] > _xs[0] else 0.0
        v = (my - _ys[0]) / (_ys[1] - _ys[0]) if _ys[1] > _ys[0] else 0.0
        return cfg.tilt_x * min(max(u, 0.0), 1.0) + cfg.tilt_y * min(max(v, 0.0), 1.0)

    # 全平面安全抬笔：抬笔用统一高度（不按位置补偿），落笔仍按位置补偿
    z_lift = safe_lift_z(cfg)

    def pen_up() -> None:
        if cfg.use_z:
            out.append(f"G1 F{cfg.z_speed:.0f} Z{z_lift:.2f}")
        elif cfg.pen_up_cmd:
            out.append(cfg.pen_up_cmd)

    def pen_down() -> None:
        if cfg.use_z:
            out.append(f"G1 F{cfg.z_down_speed:.0f} Z{cfg.z_on + z_comp(cur_x, cur_y):.2f}")
        elif cfg.pen_down_cmd:
            out.append(cfg.pen_down_cmd)

    # 开场高抬（Z-2）：从停靠点/上一任务位置出发的首段路程保持最高点，
    # 防低抬笔净空（0.8mm）跨纸刮纸——到目标点后再降。
    out.append(f"G1 F{cfg.z_speed:.0f} Z-2")
    # （已取消：每页开头先移动到停靠点——无意义路径）

    # 落笔预热：触纸 3 次慢速起落 + 压实（排墨 + 吃掉松电后 Z 空程）。
    # 必须触纸才有效——先前在停靠点空中起落，笔内弹簧不压缩、笔尖不出墨，
    # 首笔必浅（用户实报"启动后第一次落笔深度严重不足"）。
    if cfg.dot_x is not None and cfg.dot_y is not None:
        dx, dy = to_machine(cfg.dot_x, cfg.dot_y, cfg)
        out.append(f"G1 F{cfg.travel:.0f} X{dx:.2f} Y{dy:.2f}")
        cur_x, cur_y = dx, dy
    for _ in range(3):
        pen_down()
        out.append("G4 P0.5")
        pen_up()
        out.append("G4 P0.3")
    if cfg.dot_x is not None and cfg.dot_y is not None:
        # 压实一笔（略深 0.35mm 短驻留）：把残余空程一次性吃掉
        out.append(f"G1 F{cfg.z_down_speed:.0f} Z{cfg.z_on + z_comp(cur_x, cur_y) + 0.35:.2f}")
        out.append("G4 P0.2")
        pen_up()
        out.append("G4 P0.3")

    down = False
    last_mx, last_my = cfg.park_x, cfg.park_y  # 起点近似（首个空程方向判断用）
    for st in strokes:
        if len(st.points) < 2:
            continue
        x0, y0 = to_machine(*st.points[0], cfg)
        # 抬笔移到起点
        if down:
            pen_up()
            down = False
        trav = cfg.travel * (0.85 if x0 < last_mx - 1.0 else 1.0)  # 左移空程降速防震
        # 长空程（回行首/跨段）：升到最高点跨纸，防低抬笔刮纸
        _dist = ((x0 - last_mx) ** 2 + (y0 - last_my) ** 2) ** 0.5
        if _dist > 40.0:
            out.append(f"G1 F{cfg.z_speed:.0f} Z-2")
        out.append(f"G1 F{trav:.0f} X{x0:.2f} Y{y0:.2f}")
        if _dist > 40.0:
            out.append(f"G1 F{cfg.z_speed:.0f} Z{z_lift:.2f}")
        last_mx, last_my = x0, y0
        cur_x, cur_y = x0, y0   # 先定位再落笔：立体补偿按起点采样
        pen_down()
        if st.dwell_ms > 0:     # 标记/轻点类：驻留出墨（书写笔画不驻留）
            out.append(f"G4 P{st.dwell_ms / 1000.0:.3f}")
        out.append(f"G1 F{cfg.feed:.0f}")
        down = True
        run = 0.0  # 本笔连续落笔距离（直线/曲线统一按此抬笔，防长拖纸面扭曲）
        for px, py in st.points[1:]:
            mx, my = to_machine(px, py, cfg)
            seg = ((mx - cur_x) ** 2 + (my - cur_y) ** 2) ** 0.5
            if seg < 1e-9:
                continue
            ux, uy = (mx - cur_x) / seg, (my - cur_y) / seg
            remain = seg
            while remain > 1e-9:
                if run >= MAX_CONTACT_MM - 1e-9:
                    # 落笔行程到限：抬笔→沿方向微移→落笔再续（微移防定点积墨）
                    pen_up()
                    sh = min(0.3, remain)
                    out.append(f"G1 F{cfg.travel:.0f} X{cur_x + ux * sh:.2f} Y{cur_y + uy * sh:.2f}")
                    cur_x += ux * sh
                    cur_y += uy * sh
                    remain -= sh
                    pen_down()
                    out.append(f"G1 F{cfg.feed:.0f}")
                    run = 0.0
                    continue
                step = min(remain, MAX_CONTACT_MM - run)
                cur_x += ux * step
                cur_y += uy * step
                remain -= step
                run += step
                out.append(f"G1 X{cur_x:.2f} Y{cur_y:.2f}")
            last_mx, last_my = mx, my

    if down:
        pen_up()
    if final:
        out.append(f"G1 F{cfg.travel:.0f} X{cfg.park_x:.2f} Y{cfg.park_y:.2f}")
        out.append("M2")  # 程序结束（GRBL 忽略/结束）
    else:
        # 页间换纸：停右下角 → 电机空闲 25ms 自动松开（$1=25，无需 M18）→ 等推回左上角
        out.append(f"G1 F{cfg.travel:.0f} X{cfg.corner_x:.2f} Y{cfg.corner_y:.2f}")
        out.append("; @swap-page")
    return out


def pages_to_gcode(pages: list[list[Stroke]], cfg: GCodeConfig | None = None) -> list[str]:
    """多页笔画 → 一段 G-code；页间自动走换纸流程（停右下角+松电机+等推回）。"""
    cfg = cfg or GCodeConfig()
    out: list[str] = []
    for i, page in enumerate(pages):
        out.extend(strokes_to_gcode(page, cfg, final=(i == len(pages) - 1)))
    return out
