"""图片 → 手写机可画的折线笔画（黑白手绘风）。

两条路径：
- SVG 矢量：path/基本形状解析、transform/viewBox 变换、曲线按弦长误差折线化；
- 位图：chart=线稿曲线跟踪矢量化（沿轨迹游走成连续长折线），sketch=铅笔素描 + 按暗度水平排线。

输出约定：笔画为折线点列，单位 mm，原点=图框左上，y 向下，保持长宽比居中于 box。
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, uniform_filter

# 曲线折线化的弦长误差上限（mm）
CHORD_TOL = 0.05
# 折线抽稀（RDP）容差（mm）
RDP_TOL = 0.02
# 素描排线最密间距（mm，纯黑处；灰度 255 不画）
HATCH_MIN_STEP = 0.8
# 素描暗度分档数（档位越高越暗、线距越密）
HATCH_LEVELS = 8

_NUM_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
_TRANSFORM_CMD = re.compile(r"([a-zA-Z]+)\s*\(([^)]*)\)")
_LEN_RE = re.compile(r"^\s*([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)")
_SEP_RE = re.compile(r"[\s,]+")

# 不参与描摹的容器/非图形节点（含命名空间无关的局部小写名）
_SKIP_TAGS = frozenset({
    "defs", "clippath", "mask", "symbol", "marker", "title", "desc",
    "metadata", "style", "script", "lineargradient", "radialgradient",
    "pattern", "filter", "text", "image", "use",
})

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


# ---------------------------------------------------------------- 公共 API

def is_svg(data: bytes) -> bool:
    """嗅探 SVG：bytes 含 <svg 标签。"""
    if not data:
        return False
    return re.search(br"<svg[\s/>]", data, re.IGNORECASE) is not None


def svg_to_strokes(data: bytes, box_w: float, box_h: float) -> list[list[tuple[float, float]]]:
    """SVG 矢量 → 折线笔画。
    支持：path d（MmLlHhVvCcSsQqTtAaZz 全命令）、line/rect/circle/ellipse/polyline/polygon、
    viewBox 缩放、transform="translate/scale/rotate/matrix"（含嵌套 <g>）。
    描边（stroke）画出来；纯填充形状画其轮廓。曲线按弦长误差≤0.05mm 折线化。
    保持长宽比、居中放入 box（box 左上角=坐标原点，y 向下，单位 mm）。
    """
    root = ET.fromstring(data)
    if _local(root.tag) != "svg":
        # 容错：文档根不是 svg 时找第一个 svg 子元素
        found = None
        for el in root.iter():
            if _local(el.tag) == "svg":
                found = el
                break
        if found is None:
            return []
        root = found

    geoms: list[tuple] = []
    hull: list[tuple[float, float]] = []
    _collect(root, _IDENTITY, {}, geoms, hull)
    if not geoms:
        return []

    # 画源矩形：viewBox（或 width/height）与内容凸包的并集 → 保证输出落在框内
    src = _viewbox_rect(root)
    if hull:
        xs = [p[0] for p in hull]
        ys = [p[1] for p in hull]
        src = _rect_union(src, (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
    if src is None or src[2] <= 0 or src[3] <= 0:
        return []
    sx, sy, sw, sh = src
    if box_w <= 0 or box_h <= 0 or sw <= 0 or sh <= 0:
        return []
    scale = min(box_w / sw, box_h / sh)
    off_x = (box_w - sw * scale) / 2.0
    off_y = (box_h - sh * scale) / 2.0

    # 折线化公差换算到根坐标（mm 弦长误差 ≤ CHORD_TOL）
    tol = CHORD_TOL / scale if scale > 0 else CHORD_TOL
    out: list[list[tuple[float, float]]] = []
    for rec in geoms:
        pts = _flatten_rec(rec, tol)
        pts = [(off_x + (x - sx) * scale, off_y + (y - sy) * scale) for x, y in pts]
        pts = _clamp_pts(_rdp(_dedup(pts), RDP_TOL), box_w, box_h)
        if len(pts) >= 2:
            out.append(pts)
    return out


def classify_raster(img) -> str:
    """返回 'chart'（线条图/统计图表/CAD 截图）或 'sketch'（照片）。
    启发式：色彩数少+白底占比高+细线边缘多 → chart；否则 sketch。对 RGBA/L/P 模式都要鲁棒。
    """
    rgb = _to_rgb_array(img)
    if rgb.shape[0] < 2 or rgb.shape[1] < 2:
        return "sketch"
    # 色彩数：5bit/通道量化后去重；大图抽样控制 unique 成本
    step = max(1, int(math.sqrt(rgb.shape[0] * rgb.shape[1] / 262144)))
    samp = rgb[::step, ::step]
    q = (samp.astype(np.uint16) >> 3).astype(np.uint8)
    keys = (q[..., 0].astype(np.uint32) << 16) | (q[..., 1].astype(np.uint32) << 8) | q[..., 2]
    uniq, counts = np.unique(keys, return_counts=True)
    n_colors = int(uniq.size)
    top3_ratio = float(np.sort(counts)[::-1][:3].sum()) / float(counts.sum())
    few_colors = n_colors <= 96 or top3_ratio >= 0.75

    gray = _to_gray_from_rgb(rgb)
    white_ratio = float(np.mean(gray >= 245.0))

    # 细线边缘：梯度显著且局部 3x3 边缘稀疏（线稿的线细、照片的边缘成片）
    gy, gx = np.gradient(gray.astype(np.float32))
    grad = np.hypot(gx, gy)
    edge = grad > 20.0
    edge_ratio = float(edge.mean())
    if edge_ratio > 0:
        e = edge.astype(np.float32)
        thin = edge & (uniform_filter(e, size=3) * 9.0 <= 6.0)
        thin_ratio = float(thin.sum()) / max(float(edge.sum()), 1.0)
    else:
        thin_ratio = 0.0

    # 地图检测优先：大色块填充区域
    if _is_map_region(rgb):
        return "map"
    is_chart = (few_colors and white_ratio >= 0.5
                and 0.002 <= edge_ratio <= 0.6 and thin_ratio >= 0.25)
    return "chart" if is_chart else "sketch"


def raster_to_strokes(img, box_w: float, box_h: float, mode: str,
                      max_strokes: int = 6000) -> list[list[tuple[float, float]]]:
    """位图 → 折线笔画（保持长宽比、居中放入 box）。
    mode='chart'：灰度→自动对比度→大津二值→黑线曲线跟踪（沿轨迹游走成连续长折线，一条线一笔）。
    mode='sketch'：灰度→铅笔素描（大图高斯模糊减淡/dodge 混合）→按暗度水平排线（横向短线段，暗处密亮处疏，全白不画）。
    笔画数超 max_strokes 时降密度/降采样。每个折线至少 2 点。
    """
    if mode not in ("chart", "sketch", "map"):
        raise ValueError(f"未知 mode: {mode!r}（应为 'chart' / 'map' / 'sketch'）")
    gray = _to_gray(img)
    h, w = gray.shape
    if h < 1 or w < 1 or box_w <= 0 or box_h <= 0:
        return []
    max_strokes = max(1, int(max_strokes))
    if mode == "chart":
        # 多色图表→线型编码；不多色时退回单 mask
        rgb = _to_rgb_array(img)
        multi = _chart_multicolor_strokes(rgb, gray, box_w, box_h, max_strokes)
        if multi is not None:
            return multi
        strokes = _chart_strokes(gray, max_strokes)
        out = []
        for pts in strokes:
            mp = _dedup(_px_to_box(pts, w, h, box_w, box_h))
            mp = _clamp_pts(_rdp(mp, RDP_TOL), box_w, box_h)
            if len(mp) >= 2:
                out.append(mp)
        return out
    if mode == "map":
        rgb = _to_rgb_array(img)
        return _map_strokes(rgb, gray, box_w, box_h, max_strokes)
    return _sketch_strokes(gray, box_w, box_h, max_strokes)


# ---------------------------------------------------------------- SVG：矩阵与变换

def _local(tag: str) -> str:
    """去命名空间，取局部标签名（小写）。"""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _mat_mul(m: tuple, n: tuple) -> tuple:
    """齐次变换乘法 m·n（先 n 后 m）。SVG matrix(a,b,c,d,e,f)：x'=ax+cy+e。"""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def _apply(m: tuple, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _parse_transform(s: str) -> tuple:
    """transform 属性 → 齐次矩阵（列表从左到右依次作用于点）。"""
    m = _IDENTITY
    for name, args in _TRANSFORM_CMD.findall(s or ""):
        vals = [float(v) for v in _SEP_RE.split(args.strip()) if v]
        name = name.lower()
        if name == "matrix" and len(vals) >= 6:
            t = tuple(vals[:6])
        elif name == "translate":
            tx = vals[0] if vals else 0.0
            ty = vals[1] if len(vals) > 1 else 0.0
            t = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif name == "scale":
            sx = vals[0] if vals else 1.0
            sy = vals[1] if len(vals) > 1 else sx
            t = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "rotate":
            ang = math.radians(vals[0] if vals else 0.0)
            ca, sa = math.cos(ang), math.sin(ang)
            t = (ca, sa, -sa, ca, 0.0, 0.0)
            if len(vals) >= 3:
                cx, cy = vals[1], vals[2]
                t = _mat_mul((1.0, 0.0, 0.0, 1.0, cx, cy),
                             _mat_mul(t, (1.0, 0.0, 0.0, 1.0, -cx, -cy)))
        elif name == "skewx" and vals:
            t = (1.0, 0.0, math.tan(math.radians(vals[0])), 1.0, 0.0, 0.0)
        elif name == "skewy" and vals:
            t = (1.0, math.tan(math.radians(vals[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        m = _mat_mul(m, t)
    return m


# ---------------------------------------------------------------- SVG：path d 扫描

class _DScanner:
    """path d 词法扫描（arc 标志位可与相邻数字连写，需逐字符读）。"""

    def __init__(self, s: str) -> None:
        self.s = s
        self.i = 0
        self.n = len(s)

    def _skip(self) -> None:
        while self.i < self.n and self.s[self.i] in " ,\t\r\n":
            self.i += 1

    def peek_cmd(self) -> str | None:
        self._skip()
        if self.i < self.n and self.s[self.i].isalpha():
            return self.s[self.i]
        return None

    def read_cmd(self) -> str:
        self._skip()
        ch = self.s[self.i]
        self.i += 1
        return ch

    def read_num(self) -> float:
        self._skip()
        m = _NUM_RE.match(self.s, self.i)
        if not m:
            raise ValueError(f"SVG path 数字解析失败 @{self.i}")
        self.i = m.end()
        return float(m.group(0))

    def read_flag(self) -> int:
        self._skip()
        if self.i >= self.n:
            raise ValueError("SVG arc 标志位缺失")
        ch = self.s[self.i]
        self.i += 1
        return 1 if ch == "1" else 0


def _path_subpaths(d: str, mat: tuple) -> list[tuple]:
    """path d → 子路径记录列表：(start, segs, closed)。

    segs 元素：("line",(p0,p1)) / ("cubic",(p0,c1,c2,p3)) / ("quad",(p0,c1,p2))
    / ("arc",(p0,p1,rx,ry,rot,laf,sf,mat))；前三种点已变换到根坐标。
    """
    sc = _DScanner(d)
    subs: list[tuple] = []
    segs: list[tuple] | None = None
    closed = False
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    prev_c: tuple | None = None  # 上一 cubic 第二控制点（S 反射用）
    prev_q: tuple | None = None  # 上一 quad 控制点（T 反射用）
    cmd: str | None = None
    first = True

    def end_sub() -> None:
        nonlocal segs, closed
        if segs is not None:
            subs.append((start, segs, closed))
        segs = None
        closed = False

    while True:
        p = sc.peek_cmd()
        if p is not None:
            cmd = sc.read_cmd()
            first = True
        elif cmd is None or cmd in "Zz":
            break
        else:
            # 隐式重复命令：后面必须还有数字参数，否则结束
            sc._skip()
            if sc.i >= sc.n or _NUM_RE.match(sc.s, sc.i) is None:
                break
        rel = cmd.islower()
        c = cmd.upper()
        if c == "Z":
            if segs is not None:
                closed = True
                cur = start
            prev_c = prev_q = None
            cmd = None
            continue

        def rp() -> tuple[float, float]:
            x = sc.read_num()
            y = sc.read_num()
            return (cur[0] + x, cur[1] + y) if rel else (x, y)

        def tl(p: tuple[float, float]) -> tuple[float, float]:
            return _apply(mat, p[0], p[1])  # 段点须变换到根坐标（否则与起点错位出杂线）

        if segs is None:
            segs = []
            start = cur
        if c == "M":
            q = rp()
            if first:
                end_sub()
                segs = []
                start = q
                closed = False
            else:
                segs.append(("line", (tl(cur), tl(q))))  # 隐式 L
            cur = q
            prev_c = prev_q = None
        elif c == "L":
            q = rp()
            segs.append(("line", (tl(cur), tl(q))))
            cur = q
            prev_c = prev_q = None
        elif c == "H":
            x = sc.read_num()
            x = cur[0] + x if rel else x
            q = (x, cur[1])
            segs.append(("line", (tl(cur), tl(q))))
            cur = q
            prev_c = prev_q = None
        elif c == "V":
            y = sc.read_num()
            y = cur[1] + y if rel else y
            q = (cur[0], y)
            segs.append(("line", (tl(cur), tl(q))))
            cur = q
            prev_c = prev_q = None
        elif c == "C":
            c1 = rp()
            c2 = rp()
            q = rp()
            segs.append(("cubic", (_apply(mat, *cur), _apply(mat, *c1),
                                   _apply(mat, *c2), _apply(mat, *q))))
            prev_c, prev_q = c2, None
            cur = q
        elif c == "S":
            c2 = rp()
            q = rp()
            c1 = (2 * cur[0] - prev_c[0], 2 * cur[1] - prev_c[1]) if prev_c else cur
            segs.append(("cubic", (_apply(mat, *cur), _apply(mat, *c1),
                                   _apply(mat, *c2), _apply(mat, *q))))
            prev_c, prev_q = c2, None
            cur = q
        elif c == "Q":
            c1 = rp()
            q = rp()
            segs.append(("quad", (_apply(mat, *cur), _apply(mat, *c1), _apply(mat, *q))))
            prev_q, prev_c = c1, None
            cur = q
        elif c == "T":
            q = rp()
            c1 = (2 * cur[0] - prev_q[0], 2 * cur[1] - prev_q[1]) if prev_q else cur
            segs.append(("quad", (_apply(mat, *cur), _apply(mat, *c1), _apply(mat, *q))))
            prev_q, prev_c = c1, None
            cur = q
        elif c == "A":
            rx = sc.read_num()
            ry = sc.read_num()
            rot = sc.read_num()
            laf = sc.read_flag()
            sf = sc.read_flag()
            q = rp()
            segs.append(("arc", (cur, q, rx, ry, rot, laf, sf, mat)))
            prev_c = prev_q = None
            cur = q
        else:
            break
        first = False
    end_sub()
    return subs


# ---------------------------------------------------------------- SVG：弧与折线化

def _arc_center(x1: float, y1: float, rx: float, ry: float, phi_deg: float,
                fa: int, fs: int, x2: float, y2: float):
    """SVG 端点式弧 → 圆心参数化（F.6.5）。退化时返回 None。"""
    if abs(x1 - x2) < 1e-12 and abs(y1 - y2) < 1e-12:
        return None
    rx, ry = abs(rx), abs(ry)
    if rx < 1e-12 or ry < 1e-12:
        return None
    phi = math.radians(phi_deg)
    cosp, sinp = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cosp * dx2 + sinp * dy2
    y1p = -sinp * dx2 + cosp * dy2
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1.0:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den > 0 else 0.0
    if fa == fs:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cosp * cxp - sinp * cyp + (x1 + x2) / 2.0
    cy = sinp * cxp + cosp * cyp + (y1 + y2) / 2.0

    def ang(ux: float, uy: float, vx: float, vy: float) -> float:
        nn = math.hypot(ux, uy) * math.hypot(vx, vy)
        if nn <= 0:
            return 0.0
        a = math.acos(max(-1.0, min(1.0, (ux * vx + uy * vy) / nn)))
        return -a if ux * vy - uy * vx < 0 else a

    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    theta1 = ang(1.0, 0.0, ux, uy)
    dtheta = ang(ux, uy, vx, vy)
    if fs == 0 and dtheta > 0:
        dtheta -= 2 * math.pi
    if fs == 1 and dtheta < 0:
        dtheta += 2 * math.pi
    return cx, cy, rx, ry, phi, theta1, dtheta


def _flat_cubic(p0, p1, p2, p3, tol: float, out: list, depth: int = 0) -> None:
    """de Casteljau 对分折线化；控制点距弦 ≤ tol 即收（曲线偏差 ≤ 0.75·tol）。"""
    if depth >= 18:
        out.append(p3)
        return
    if (_dist_pt_seg(p1, p0, p3) <= tol and _dist_pt_seg(p2, p0, p3) <= tol):
        out.append(p3)
        return

    def mid(a, b):
        return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)

    p01 = mid(p0, p1)
    p12 = mid(p1, p2)
    p23 = mid(p2, p3)
    p012 = mid(p01, p12)
    p123 = mid(p12, p23)
    pm = mid(p012, p123)
    _flat_cubic(p0, p01, p012, pm, tol, out, depth + 1)
    _flat_cubic(pm, p123, p23, p3, tol, out, depth + 1)


def _dist_pt_seg(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 <= 1e-18:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _flat_arc(seg, tol_root: float, out: list) -> None:
    """arc 段 → 折线（用户空间按弦高采样后变换；弦长误差 ≤ tol_root）。"""
    p0, p1, rx, ry, rot, laf, sf, mat = seg
    ac = _arc_center(p0[0], p0[1], rx, ry, rot, laf, sf, p1[0], p1[1])
    if ac is None:
        out.append(_apply(mat, *p1))
        return
    cx, cy, rx, ry, phi, t1, dt = ac
    a, b, c, d, _e, _f = mat
    # 仿射线性部分的谱范数上界：Frobenius 范数（保守，折线只会更细）
    smax = math.sqrt(a * a + b * b + c * c + d * d) or 1e-12
    tol_user = tol_root / smax
    rmax = max(rx, ry)
    if rmax <= tol_user:
        out.append(_apply(mat, *p1))
        return
    step = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - tol_user / rmax)))
    if step <= 1e-6:
        step = 1e-6
    n = max(2, min(4096, int(math.ceil(abs(dt) / step))))
    cosp, sinp = math.cos(phi), math.sin(phi)
    for i in range(1, n):
        t = t1 + dt * i / n
        ct, st = math.cos(t), math.sin(t)
        ux = cx + rx * ct * cosp - ry * st * sinp
        uy = cy + rx * ct * sinp + ry * st * cosp
        out.append(_apply(mat, ux, uy))
    out.append(_apply(mat, *p1))  # 终点精确落在原弧端点（避免采样漂移）


# ---------------------------------------------------------------- SVG：形状收集

def _parse_style(el) -> dict[str, str]:
    st: dict[str, str] = {}
    for k in ("fill", "stroke", "display", "visibility"):
        v = el.get(k)
        if v is not None:
            st[k] = v.strip()
    raw = el.get("style")
    if raw:
        for part in raw.split(";"):
            if ":" in part:
                k, _, v = part.partition(":")
                st[k.strip().lower()] = v.strip()
    return st


def _nums(s: str) -> list[float]:
    return [float(v) for v in _SEP_RE.split((s or "").strip()) if v]


def _collect(el, mat: tuple, style: dict, geoms: list, hull: list) -> None:
    """遍历元素树，收集曲线记录与凸包锚点（根坐标）。"""
    tag = _local(el.tag)
    if tag in _SKIP_TAGS:
        return
    st = dict(style)
    st.update(_parse_style(el))
    if st.get("display", "").lower() == "none" or st.get("visibility", "").lower() == "hidden":
        return
    own = el.get("transform")
    m = _mat_mul(mat, _parse_transform(own)) if own else mat

    if tag in ("g", "svg", "a", "switch"):
        for ch in el:
            _collect(ch, m, st, geoms, hull)
        return

    fill = st.get("fill", "black").strip().lower()
    stroke = st.get("stroke", "none").strip().lower()
    has_fill = fill not in ("none", "", "transparent")
    has_stroke = stroke not in ("none", "", "transparent")
    # line 无面积，仅描边可画；其余形状：描边或纯填充（画轮廓）都画
    if tag == "line":
        if not has_stroke:
            return
    elif not (has_fill or has_stroke):
        return

    def add_sub(start, segs, closed: bool) -> None:
        if not segs:
            return
        eff_closed = closed or has_fill  # 纯填充形状画其轮廓（隐式闭合）
        geoms.append((start, segs, eff_closed))
        hull.append(_apply(m, *start))
        for sgm in segs:
            k = sgm[0]
            if k == "line":
                hull.append(sgm[1][0])
                hull.append(sgm[1][1])
            elif k in ("cubic", "quad"):
                hull.extend(sgm[1])
            elif k == "arc":
                _arc_hull(sgm[1], hull)

    if tag == "path":
        for start, segs, closed in _path_subpaths(el.get("d") or "", m):
            add_sub(_apply(m, *start), segs, closed)
    elif tag == "line":
        p0 = (float(el.get("x1", 0)), float(el.get("y1", 0)))
        p1 = (float(el.get("x2", 0)), float(el.get("y2", 0)))
        add_sub(_apply(m, *p0), [("line", (_apply(m, *p0), _apply(m, *p1)))], False)
    elif tag == "rect":
        x = float(el.get("x", 0))
        y = float(el.get("y", 0))
        w = float(el.get("width", 0))
        h = float(el.get("height", 0))
        if w <= 0 or h <= 0:
            return
        rx = el.get("rx")
        ry = el.get("ry")
        if rx is None and ry is None:
            pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            segs = [("line", (_apply(m, *pts[i]), _apply(m, *pts[i + 1])))
                    for i in range(3)]
            segs.append(("line", (_apply(m, *pts[3]), _apply(m, *pts[0]))))
            add_sub(_apply(m, *pts[0]), segs, True)
        else:
            rxv = float(rx) if rx is not None else float(ry)
            ryv = float(ry) if ry is not None else rxv
            rxv = min(max(rxv, 0.0), w / 2.0)
            ryv = min(max(ryv, 0.0), h / 2.0)
            _rect_rounded_segs(x, y, w, h, rxv, ryv, m, add_sub)
    elif tag in ("polyline", "polygon"):
        vals = _nums(el.get("points"))
        pts = list(zip(vals[0::2], vals[1::2]))
        if len(pts) < 2:
            return
        segs = [("line", (_apply(m, *pts[i]), _apply(m, *pts[i + 1])))
                for i in range(len(pts) - 1)]
        if tag == "polygon":
            segs.append(("line", (_apply(m, *pts[-1]), _apply(m, *pts[0]))))
        add_sub(_apply(m, *pts[0]), segs, tag == "polygon")
    elif tag in ("circle", "ellipse"):
        cx = float(el.get("cx", 0))
        cy = float(el.get("cy", 0))
        if tag == "circle":
            r = float(el.get("r", 0))
            rx = ry = r
        else:
            rx = float(el.get("rx", 0))
            ry = float(el.get("ry", 0))
        if rx <= 0 or ry <= 0:
            return
        _ellipse_segs(cx, cy, rx, ry, m, add_sub)


def _rect_rounded_segs(x, y, w, h, rx, ry, mat, add_sub) -> None:
    """圆角矩形：四段直线 + 四段 90° 椭圆弧。"""
    p = {
        "tl": (x + rx, y), "tr": (x + w - rx, y),
        "br": (x + w, y + h - ry), "bl": (x, y + h - ry),
        "bl2": (x + rx, y + h), "br2": (x + w - rx, y + h),
        "tr2": (x + w, y + ry), "tl2": (x, y + ry),
    }
    segs = [
        ("line", (_apply(mat, *p["tl"]), _apply(mat, *p["tr"]))),
        ("arc", (p["tr"], p["tr2"], rx, ry, 0.0, 0, 1, mat)),
        ("line", (_apply(mat, *p["tr2"]), _apply(mat, *p["br"]))),
        ("arc", (p["br"], p["br2"], rx, ry, 0.0, 0, 1, mat)),
        ("line", (_apply(mat, *p["br2"]), _apply(mat, *p["bl2"]))),
        ("arc", (p["bl2"], p["bl"], rx, ry, 0.0, 0, 1, mat)),
        ("line", (_apply(mat, *p["bl"]), _apply(mat, *p["tl2"]))),
        ("arc", (p["tl2"], p["tl"], rx, ry, 0.0, 0, 1, mat)),
    ]
    add_sub(_apply(mat, *p["tl"]), segs, True)


def _ellipse_segs(cx, cy, rx, ry, mat, add_sub) -> None:
    """整椭圆 → 两段 180° 弧（闭合轮廓）。"""
    p0 = (cx - rx, cy)
    p1 = (cx + rx, cy)
    segs = [
        ("arc", (p0, p1, rx, ry, 0.0, 1, 1, mat)),
        ("arc", (p1, p0, rx, ry, 0.0, 1, 1, mat)),
    ]
    add_sub(_apply(mat, *p0), segs, True)


def _arc_hull(seg, hull: list) -> None:
    """arc 凸包锚点：端点 + 椭圆包围盒四角（仿射像覆盖曲线像）。"""
    p0, p1, rx, ry, rot, laf, sf, mat = seg
    hull.append(_apply(mat, *p0))
    hull.append(_apply(mat, *p1))
    ac = _arc_center(p0[0], p0[1], rx, ry, rot, laf, sf, p1[0], p1[1])
    if ac is None:
        return
    cx, cy, rx, ry, phi, _t1, _dt = ac
    hw = math.sqrt((rx * math.cos(phi)) ** 2 + (ry * math.sin(phi)) ** 2)
    hh = math.sqrt((rx * math.sin(phi)) ** 2 + (ry * math.cos(phi)) ** 2)
    for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        hull.append(_apply(mat, cx + dx, cy + dy))


def _flatten_rec(rec, tol: float) -> list[tuple[float, float]]:
    """曲线记录 → 根坐标折线（含起点，闭合则补回首点）。"""
    start, segs, closed = rec
    pts: list[tuple[float, float]] = [start]
    for sgm in segs:
        k = sgm[0]
        if k == "line":
            pts.append(sgm[1][1])
        elif k == "cubic":
            _flat_cubic(sgm[1][0], sgm[1][1], sgm[1][2], sgm[1][3], tol, pts)
        elif k == "quad":
            p0, c1, p2 = sgm[1]
            # quad → cubic 精确提升后复用折线化
            c1a = (p0[0] + 2.0 / 3.0 * (c1[0] - p0[0]), p0[1] + 2.0 / 3.0 * (c1[1] - p0[1]))
            c2a = (p2[0] + 2.0 / 3.0 * (c1[0] - p2[0]), p2[1] + 2.0 / 3.0 * (c1[1] - p2[1]))
            _flat_cubic(p0, c1a, c2a, p2, tol, pts)
        elif k == "arc":
            _flat_arc(sgm[1], tol, pts)
    if closed and pts and (abs(pts[0][0] - pts[-1][0]) > 1e-9 or abs(pts[0][1] - pts[-1][1]) > 1e-9):
        pts.append(pts[0])
    return pts


def _viewbox_rect(root) -> tuple[float, float, float, float] | None:
    vb = root.get("viewBox") or root.get("viewbox")
    if vb:
        v = _nums(vb)
        if len(v) >= 4 and v[2] > 0 and v[3] > 0:
            return (v[0], v[1], v[2], v[3])
    w = _parse_len(root.get("width"))
    h = _parse_len(root.get("height"))
    if w > 0 and h > 0:
        return (0.0, 0.0, w, h)
    return None


def _parse_len(s: str | None) -> float:
    if not s:
        return 0.0
    m = _LEN_RE.match(s)
    return float(m.group(1)) if m else 0.0


def _rect_union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    x0 = min(a[0], b[0])
    y0 = min(a[1], b[1])
    x1 = max(a[0] + a[2], b[0] + b[2])
    y1 = max(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0)


# ---------------------------------------------------------------- 位图：预处理

def _to_rgb_array(img) -> np.ndarray:
    """任意模式（含 RGBA/L/P）→ RGB uint8 数组；透明合成到白底。"""
    if not hasattr(img, "convert"):
        img = Image.fromarray(np.asarray(img))
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba).convert("RGB")
    else:
        img = img.convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def _to_gray_from_rgb(rgb: np.ndarray) -> np.ndarray:
    return (rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114).astype(np.float32)


def _to_gray(img) -> np.ndarray:
    return _to_gray_from_rgb(_to_rgb_array(img))


def _auto_contrast(gray: np.ndarray) -> np.ndarray:
    """1%~99% 分位拉伸到 0..255。"""
    lo, hi = np.percentile(gray, (1.0, 99.0))
    if hi <= lo + 1e-6:
        return gray
    return np.clip((gray - lo) * (255.0 / (hi - lo)), 0.0, 255.0)


def _otsu(gray_u8: np.ndarray) -> int:
    """大津阈值（类间方差最大），返回 t 使 gray<=t 为墨。"""
    hist = np.bincount(gray_u8.ravel().astype(np.int64), minlength=256).astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return 127
    w0 = np.cumsum(hist)
    w1 = total - w0
    centers = np.arange(256, dtype=np.float64)
    sum_all = float((hist * centers).sum())
    sum0 = np.cumsum(hist * centers)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean0 = np.divide(sum0, w0, out=np.zeros(256), where=w0 > 0)
        mean1 = np.divide(sum_all - sum0, w1, out=np.zeros(256), where=w1 > 0)
    var_b = w0 * w1 * (mean0 - mean1) ** 2
    var_b[(w0 <= 0) | (w1 <= 0)] = -1.0
    return int(np.argmax(var_b))


def _px_to_box(pts, w: int, h: int, box_w: float, box_h: float):
    """像素坐标（左上原点）→ 框内 mm（像素中心、等比居中）。"""
    s = min(box_w / w, box_h / h) if w > 0 and h > 0 else 0.0
    ox = (box_w - w * s) / 2.0
    oy = (box_h - h * s) / 2.0
    return [(ox + (x + 0.5) * s, oy + (y + 0.5) * s) for x, y in pts]


def _dedup(pts):
    if not pts:
        return []
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) > 1e-9 or abs(p[1] - out[-1][1]) > 1e-9:
            out.append(p)
    return out


def _clamp_pts(pts, box_w: float, box_h: float):
    return [(min(max(x, 0.0), box_w), min(max(y, 0.0), box_h)) for x, y in pts]




def _is_map_region(rgb: np.ndarray) -> bool:
    """判断地图：彩色连通分量（面积≥2% 且 填充率≥0.45）≥2 且彩色覆盖≥15%。"""
    from scipy.ndimage import label as _ndlabel
    h_arr, s_arr, v_arr = _rgb_to_hsv(rgb)
    white_mask = (v_arr > 0.95) & (s_arr < 0.08)
    black_mask = (v_arr < 0.2) | ((s_arr < 0.1) & (v_arr < 0.8))
    colored_mask = (s_arr >= 0.25) & ~white_mask & ~black_mask

    total_px = rgb.shape[0] * rgb.shape[1]
    if total_px == 0:
        return False
    colored_coverage = float(colored_mask.sum()) / total_px
    if colored_coverage < 0.15:
        return False

    labeled, n = _ndlabel(colored_mask)
    big_count = 0
    for comp_id in range(1, n + 1):
        comp = labeled == comp_id
        comp_area = int(comp.sum())
        if comp_area < 0.02 * total_px:
            continue
        ys, xs = np.nonzero(comp)
        bw = int(xs.max() - xs.min() + 1)
        bh = int(ys.max() - ys.min() + 1)
        fill_ratio = comp_area / max(bw * bh, 1)
        if fill_ratio >= 0.45:
            big_count += 1
    return big_count >= 2


# ---------------------------------------------------------------- 位图：HSV 与多色聚类

def _rgb_to_hsv(rgb: np.ndarray):
    """RGB uint8 (...,3) → H[0,360), S[0,1], V[0,1] float arrays."""
    arr = rgb.astype(np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    maxc = np.max(arr, axis=-1)
    minc = np.min(arr, axis=-1)
    v = maxc
    diff = maxc - minc
    s = np.where(maxc > 0, diff / np.maximum(maxc, 1e-8), 0.0)
    rc = (maxc - r) / np.maximum(diff, 1e-8)
    gc = (maxc - g) / np.maximum(diff, 1e-8)
    bc = (maxc - b) / np.maximum(diff, 1e-8)
    h = np.zeros_like(v)
    h = np.where(maxc == r, bc - gc, h)
    h = np.where(maxc == g, 2.0 + rc - bc, h)
    h = np.where(maxc == b, 4.0 + gc - rc, h)
    h = (h / 6.0) % 1.0
    h = h * 360.0
    return h, s, v


def _cluster_series(rgb: np.ndarray):
    """多色图表系列聚类（HSV 色相直方图）。
    返回 (clusters, black_mask, ink_total)：
    clusters = [(mask, hue_deg, area_px), ...] 按面积降序（仅彩色）；
    black_mask = 近黑/无彩灰像素；ink_total = 总墨像素数。
    """
    h_arr, s_arr, v_arr = _rgb_to_hsv(rgb)

    # 近黑(V<0.2) + 无彩灰(S<0.1, V<0.8) → 黑组（坐标轴/文字，始终实线）
    black_mask = (v_arr < 0.2) | ((s_arr < 0.1) & (v_arr < 0.8))

    # 近白(V>0.95, S<0.08) → 背景，排除
    white_mask = (v_arr > 0.95) & (s_arr < 0.08)

    # 彩色像素：S≥0.25
    colored_mask = (s_arr >= 0.25) & ~white_mask & ~black_mask

    ink_mask = black_mask | colored_mask
    ink_total = int(ink_mask.sum())
    colored_total = int(colored_mask.sum())

    if colored_total < 40:
        return [], black_mask, ink_total

    # 色直方图 36 箿 = 10°/箿
    bin_map = np.full(h_arr.shape, -1, dtype=np.int32)
    bin_map[colored_mask] = np.clip((h_arr[colored_mask] / 10.0).astype(np.int32), 0, 35)
    hist = np.bincount(bin_map[colored_mask], minlength=36).astype(np.float64)

    # 峰值：≥3% 墨份额且≥40px
    threshold = max(40, 0.03 * colored_total)
    peaks = []
    for i in range(36):
        if hist[i] < threshold:
            continue
        if hist[i] >= hist[(i - 1) % 36] and hist[i] >= hist[(i + 1) % 36]:
            peaks.append((i, hist[i]))

    if not peaks:
        return [], black_mask, ink_total

    # 合并 <20° 峰（圆周距离 ≤2 箿）
    used = set()
    merged = []
    for i, (bi, ci) in enumerate(peaks):
        if i in used:
            continue
        group_bins = [bi]
        group_cnt = ci
        for j in range(i + 1, len(peaks)):
            if j in used:
                continue
            bj, cj = peaks[j]
            diff = min(abs(bi - bj), 36 - abs(bi - bj))
            if diff <= 2:
                group_bins.append(bj)
                group_cnt += cj
                used.add(j)
        sin_sum = sum(hist[b] * math.sin(math.radians(b * 10 + 5)) for b in group_bins)
        cos_sum = sum(hist[b] * math.cos(math.radians(b * 10 + 5)) for b in group_bins)
        hue_center = math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0
        merged.append((hue_center, group_cnt, group_bins))
        used.add(i)

    merged.sort(key=lambda x: -x[1])
    merged = merged[:6]
    # 丢弃 <5% 份额
    merged = [m for m in merged if m[1] >= 0.05 * colored_total]

    clusters = []
    for hue_center, cnt, bins in merged:
        mask = np.zeros_like(colored_mask)
        for b in bins:
            mask |= (bin_map == b) & colored_mask
        clusters.append((mask, hue_center, int(cnt)))

    return clusters, black_mask, ink_total


# ---------------------------------------------------------------- 线型编码（PR1）

_LINE_STYLE_PATTERNS = {
    "dash": [(3.0, 1.5)],
    "dot": [(0.6, 1.2)],
    "dashdot": [(3.0, 1.2), (0.5, 1.2)],
}


def _extract_segment(pts, cum, start, end):
    """从弧长 start 到 end 提取子折线。"""
    if end <= start or len(pts) < 2:
        return []
    result = []
    for i in range(len(pts) - 1):
        s0, s1 = cum[i], cum[i + 1]
        if s1 <= start:
            continue
        if s0 >= end:
            break
        seg_len = s1 - s0
        if seg_len <= 1e-12:
            continue
        t0 = max(0.0, (start - s0) / seg_len)
        t1 = min(1.0, (end - s0) / seg_len)
        p0 = (pts[i][0] + t0 * (pts[i + 1][0] - pts[i][0]),
              pts[i][1] + t0 * (pts[i + 1][1] - pts[i][1]))
        p1 = (pts[i][0] + t1 * (pts[i + 1][0] - pts[i][0]),
              pts[i][1] + t1 * (pts[i + 1][1] - pts[i][1]))
        if not result or abs(p0[0] - result[-1][0]) > 1e-9 or abs(p0[1] - result[-1][1]) > 1e-9:
            result.append(p0)
        result.append(p1)
    return result if len(result) >= 2 else []


def _line_style_split(pts, style_name):
    """按线型分割折线 → 多条笔画（每条实线段为一条）。"""
    if style_name == "solid" or len(pts) < 2:
        return [pts]
    pattern = _LINE_STYLE_PATTERNS.get(style_name)
    if not pattern:
        return [pts]

    cum = [0.0]
    for i in range(len(pts) - 1):
        cum.append(cum[-1] + math.dist(pts[i], pts[i + 1]))
    total = cum[-1]
    if total <= 1e-9:
        return [pts]

    out = []
    pos = 0.0
    pi = 0
    while pos < total - 1e-9:
        draw_len, gap_len = pattern[pi % len(pattern)]
        draw_end = min(pos + draw_len, total)
        seg = _extract_segment(pts, cum, pos, draw_end)
        if len(seg) >= 2:
            out.append(seg)
        pos = draw_end + gap_len
        pi += 1
    return out if out else [pts]


def _legend_swatches(cluster_mask, style_name, s, ox, oy, box_w, box_h):
    """图例色块（20-800 px²）→ 轮廓框 + 2 条线型短线。"""
    from scipy.ndimage import label as _ndlabel
    labeled, n = _ndlabel(cluster_mask)
    out = []
    for comp_id in range(1, n + 1):
        comp_mask = labeled == comp_id
        area = int(comp_mask.sum())
        if area < 20 or area > 800:
            continue
        ys, xs = np.nonzero(comp_mask)
        bw = int(xs.max() - xs.min() + 1)
        bh = int(ys.max() - ys.min() + 1)
        # 实心紧凑块：填充率≥0.75、宽高比 0.4~2.5
        fill_ratio = area / max(bw * bh, 1)
        if fill_ratio < 0.75:
            continue
        aspect = bw / max(bh, 1)
        if not (0.4 <= aspect <= 2.5):
            continue
        cx_mm = ox + (float(xs.mean()) + 0.5) * s
        cy_mm = oy + (float(ys.mean()) + 0.5) * s

        w_mm, h_mm = 8.0, 4.0
        x0 = min(max(cx_mm - w_mm / 2, 0.0), max(box_w - w_mm, 0.0))
        y0 = min(max(cy_mm - h_mm / 2, 0.0), max(box_h - h_mm, 0.0))
        x1, y1 = x0 + w_mm, y0 + h_mm

        out.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])

        # 2 条平行短线，按线型分割
        for frac in (0.35, 0.65):
            ly = y0 + h_mm * frac
            line = [(x0 + 1.0, ly), (x1 - 1.0, ly)]
            out.extend(_line_style_split(line, style_name))
    return out


# ---------------------------------------------------------------- 位图：chart 曲线跟踪

def _find_runs(ink: np.ndarray) -> list[tuple[int, int, int]]:
    """逐行黑游程 → (y, x0, x1) 闭区间。"""
    runs: list[tuple[int, int, int]] = []
    for y in range(ink.shape[0]):
        row = ink[y]
        if not row.any():
            continue
        d = np.diff(row.astype(np.int8), prepend=np.int8(0), append=np.int8(0))
        starts = np.flatnonzero(d == 1)
        ends = np.flatnonzero(d == -1) - 1
        for x0, x1 in zip(starts.tolist(), ends.tolist()):
            runs.append((y, int(x0), int(x1)))
    return runs


def _rdp(pts, tol: float):
    """Ramer-Douglas-Peucker 抽稀（端点保留，折线仍连续）。"""
    arr = np.asarray(pts, dtype=np.float64)
    n = arr.shape[0]
    if n < 3:
        return list(pts)
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        a = arr[i0]
        seg = arr[i1] - a
        l2 = float(seg @ seg)
        sub = arr[i0 + 1:i1] - a
        if l2 <= 1e-18:
            d = np.hypot(sub[:, 0], sub[:, 1])
        else:
            t = np.clip((sub @ seg) / l2, 0.0, 1.0)
            proj = a + t[:, None] * seg
            diff = arr[i0 + 1:i1] - proj
            d = np.hypot(diff[:, 0], diff[:, 1])
        k = int(np.argmax(d))
        if float(d[k]) > tol:
            idx = i0 + 1 + k
            keep[idx] = True
            stack.append((i0, idx))
            stack.append((idx, i1))
    return [tuple(float(v) for v in p) for p in arr[keep]]


def _trace_ink(ink: np.ndarray) -> list[list[tuple[float, float]]]:
    """黑线曲线跟踪：从端点起步沿方向游走成连续长折线（交叉处轻微跟线可接受）。

    一条线（折线/曲线/直线）尽量一笔走完；分叉的支线因前向像素被占而断开另起一笔。
    """
    h, w = ink.shape
    ys, xs = np.nonzero(ink)
    if ys.size == 0:
        return []
    # 8 连通暗邻居数：≤2 的像素（端点/拐角）优先作为笔画起点
    pad = np.zeros((h + 2, w + 2), dtype=np.int32)
    pad[1:-1, 1:-1] = ink.astype(np.int32)
    cnt = np.zeros((h, w), dtype=np.int32)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            if dy == 1 and dx == 1:
                continue
            cnt += pad[dy:dy + h, dx:dx + w]
    visited = np.zeros((h, w), dtype=bool)

    def cands(cy: int, cx: int, dxy, min_dot: float, max_pred: float = 2.8):
        """前向（min_dot）+ 预测窗内的未访问墨点：(y, x, dx, dy, dist_pred, perp, dist_cur)。"""
        px = cx + dxy[0] * 1.1
        py = cy + dxy[1] * 1.1
        y0, y1 = max(0, int(py) - 3), min(h - 1, int(py) + 3)
        x0, x1 = max(0, int(px) - 3), min(w - 1, int(px) + 3)
        out = []
        for yy in range(y0, y1 + 1):
            for xx in range(x0, x1 + 1):
                if (yy == cy and xx == cx) or not ink[yy, xx] or visited[yy, xx]:
                    continue
                dx, dy = float(xx - cx), float(yy - cy)
                if dx * dxy[0] + dy * dxy[1] < min_dot:
                    continue
                dp = math.hypot(xx - px, yy - py)
                if dp > max_pred:
                    continue
                # dxy 为单位向量：叉积模长即到方向线的垂距（垂距小=沿轨迹）
                perp = abs(dx * dxy[1] - dy * dxy[0])
                out.append((yy, xx, dx, dy, dp, perp, math.hypot(dx, dy)))
        return out

    def walk(sy: int, sx: int, dxy) -> list[tuple[int, int]]:
        chain: list[tuple[int, int]] = []
        cy, cx, d = sy, sx, dxy
        while True:
            cs = cands(cy, cx, d, 0.3)
            if cs:
                # 贴身步（8 邻域）绝不跳点：防浅斜线阶梯像素被跨过留下碎段
                imm = [c for c in cs if c[6] <= 1.45]
                pool = imm or cs
                if imm:
                    c = min(pool, key=lambda z: (z[4], z[5]))          # 近者优先
                else:
                    c = min(pool, key=lambda z: z[5] + 0.4 * z[4])     # 跨点：垂距小者优先
            else:
                cs = cands(cy, cx, d, -0.2)  # 拐角兜底（直行无路时允许转弯）
                if not cs:
                    break
                # 候选反向对峙=分叉路口：停笔，支线另起一笔
                fork = False
                for i in range(len(cs)):
                    for j in range(i + 1, len(cs)):
                        a, b = cs[i], cs[j]
                        la = math.hypot(a[2], a[3]) or 1.0
                        lb = math.hypot(b[2], b[3]) or 1.0
                        if (a[2] * b[2] + a[3] * b[3]) / (la * lb) < -0.3:
                            fork = True
                            break
                    if fork:
                        break
                if fork:
                    break
                c = min(cs, key=lambda z: z[5] + 0.4 * z[4])
            ny, nx = c[0], c[1]
            dx, dy = c[2], c[3]
            ln = math.hypot(dx, dy) or 1.0
            nd = (dx / ln, dy / ln)
            d = (0.55 * d[0] + 0.45 * nd[0], 0.55 * d[1] + 0.45 * nd[1])  # 方向平滑
            ln2 = math.hypot(d[0], d[1]) or 1.0
            d = (d[0] / ln2, d[1] / ln2)
            chain.append((ny, nx))
            visited[ny, nx] = True
            cy, cx = ny, nx
        return chain

    def init_dir(sy: int, sx: int):
        best = None
        bd = 1e18
        for yy in range(max(0, sy - 3), min(h, sy + 4)):
            for xx in range(max(0, sx - 3), min(w, sx + 4)):
                if (yy == sy and xx == sx) or not ink[yy, xx] or visited[yy, xx]:
                    continue
                dist = math.hypot(float(xx - sx), float(yy - sy))
                if dist < bd:
                    bd = dist
                    best = (float(xx - sx), float(yy - sy))
        if best is None:
            return None
        ln = math.hypot(best[0], best[1]) or 1.0
        return (best[0] / ln, best[1] / ln)

    strokes: list[list[tuple[float, float]]] = []
    order = sorted(range(ys.size), key=lambda i: (int(cnt[ys[i], xs[i]]), int(ys[i]), int(xs[i])))
    for i in order:
        sy, sx = int(ys[i]), int(xs[i])
        if visited[sy, sx]:
            continue
        d0 = init_dir(sy, sx)
        if d0 is None:
            visited[sy, sx] = True  # 孤点不成笔画
            continue
        visited[sy, sx] = True
        fwd = walk(sy, sx, d0)
        bwd = walk(sy, sx, (-d0[0], -d0[1]))
        pts = [(float(x), float(y)) for y, x in reversed(bwd)]
        pts.append((float(sx), float(sy)))
        pts.extend((float(x), float(y)) for y, x in fwd)
        if len(pts) >= 2:
            strokes.append(pts)
    return strokes


def _pool_any(ink: np.ndarray, f: int) -> np.ndarray:
    """f×f 块内任一墨 → 墨（降采样保线）。"""
    h, w = ink.shape
    ph, pw = (h + f - 1) // f, (w + f - 1) // f
    pad = np.zeros((ph * f, pw * f), dtype=bool)
    pad[:h, :w] = ink
    return pad.reshape(ph, f, pw, f).any(axis=(1, 3))


def _poly_len(pts) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _chart_strokes(gray: np.ndarray, max_strokes: int) -> list[list[tuple[float, float]]]:
    g = _auto_contrast(gray)
    t = _otsu(np.clip(g, 0, 255).astype(np.uint8))
    ink = g <= float(t)
    if float(ink.mean()) > 0.85:  # 黑底白线截图 → 反相
        ink = ~ink

    def vec(mask: np.ndarray) -> list[list[tuple[float, float]]]:
        # 沿轨迹游走：一条折线/曲线/直线输出一条连续长折线
        return [pts for pts in _trace_ink(mask) if len(pts) >= 2]

    strokes = vec(ink)
    f = 1
    while len(strokes) > max_strokes and f < 16:
        f *= 2
        pooled = _pool_any(ink, f)
        strokes = [[(x * f, y * f) for x, y in pts] for pts in vec(pooled)]
    if len(strokes) > max_strokes:  # 仍超 → 降密度（留最长）
        strokes.sort(key=_poly_len, reverse=True)
        strokes = strokes[:max_strokes]
    return strokes



# ---------------------------------------------------------------- 位图：multi-color chart

def _chart_multicolor_strokes(rgb: np.ndarray, gray: np.ndarray,
                              box_w: float, box_h: float, max_strokes: int):
    """多色图表 → 线型编码笔画；不是多色时返回 None（退回单 mask）。"""
    h, w = gray.shape
    s = min(box_w / w, box_h / h) if w > 0 and h > 0 else 0.0
    if s <= 0:
        return None
    ox = (box_w - w * s) / 2.0
    oy = (box_h - h * s) / 2.0

    clusters, black_mask, ink_total = _cluster_series(rgb)
    if ink_total == 0:
        return None

    # 多色判定：≥2 簇（含黑组）各≥2% 墨量
    all_groups = [(black_mask, -1.0, int(black_mask.sum()))] + clusters
    big = [g for g in all_groups if g[2] >= 0.02 * ink_total and g[0].any()]
    if len(big) < 2:
        return None  # 回退单 mask 管线

    big.sort(key=lambda x: -x[2])
    style_cycle = ["solid", "dash", "dot", "dashdot"]

    def vec(mask):
        return [pts for pts in _trace_ink(mask) if len(pts) >= 2]

    all_strokes = []
    for idx, (mask, hue, area) in enumerate(big):
        style_name = style_cycle[idx % len(style_cycle)]
        for pts in vec(mask):
            mp = _dedup(_px_to_box(pts, w, h, box_w, box_h))
            mp = _clamp_pts(_rdp(mp, RDP_TOL), box_w, box_h)
            if len(mp) >= 2:
                all_strokes.extend(_line_style_split(mp, style_name))
        # 图例色块（仅彩色簇）
        if hue >= 0:
            all_strokes.extend(_legend_swatches(mask, style_name, s, ox, oy, box_w, box_h))

    if len(all_strokes) > max_strokes:
        all_strokes.sort(key=_poly_len, reverse=True)
        all_strokes = all_strokes[:max_strokes]
    return all_strokes


# ---------------------------------------------------------------- 位图：sketch 排线

def _sketch_strokes(gray: np.ndarray, box_w: float, box_h: float,
                    max_strokes: int) -> list[list[tuple[float, float]]]:
    h, w = gray.shape
    # 铅笔素描：反相大图高斯模糊 + dodge（除法）混合 → 白底黑轮廓
    sigma = max(3.0, max(h, w) * 0.03)
    blur = gaussian_filter(255.0 - gray.astype(np.float32), sigma=sigma)
    sketch = np.clip(gray * 255.0 / np.maximum(255.0 - blur, 1.0), 0.0, 255.0)

    # 排线密度按亮度(luminance)分档：黑≈HATCH_MIN_STEP、灰 2-4mm、白不画；
    # dodge 素描只作轮廓叠加（边缘提到最密档），保证圆形等轮廓可辨
    k_max = HATCH_LEVELS
    level = np.clip(np.ceil((255.0 - np.clip(gray, 0.0, 255.0)) * k_max / 255.0),
                    0, k_max).astype(np.int32)
    level = np.maximum(level, np.where(sketch < 210.0, k_max, 0))

    s = min(box_w / w, box_h / h) if w > 0 and h > 0 else 0.0
    if s <= 0:
        return []
    ox = (box_w - w * s) / 2.0
    oy = (box_h - h * s) / 2.0
    rng = np.random.default_rng(42)  # 固定种子：输出可复现

    def build(step_min: float) -> list[list[tuple[float, float]]]:
        out: list[list[tuple[float, float]]] = []
        img_h_mm = h * s
        for k in range(1, k_max + 1):
            # 档位线距按 1/暗度² 映射：暗档≈step_min、中灰≈2-4mm、亮档极疏
            step = step_min * (k_max / (k - 0.5)) ** 2
            n_slots = int(img_h_mm / step) + 1
            for i in range(n_slots):
                y_local = i * step
                if y_local >= img_h_mm:
                    break
                r = int(round(y_local / s - 0.5))
                r = min(max(r, 0), h - 1)
                mask = level[r] == k
                if not mask.any():
                    continue
                for _y, x0, x1 in _find_runs(mask[None, :]):
                    jx = float(rng.uniform(-0.15, 0.15))
                    jy = float(rng.uniform(-0.15, 0.15))
                    y_mm = oy + (r + 0.5) * s + jy
                    p0 = (ox + (x0 + 0.5) * s + jx, y_mm)
                    p1 = (ox + (x1 + 0.5) * s + jx, y_mm)
                    mp = _clamp_pts(_dedup([p0, p1]), box_w, box_h)
                    if len(mp) >= 2:
                        out.append(mp)
        return out

    step_min = HATCH_MIN_STEP
    strokes = build(step_min)
    it = 0
    while len(strokes) > max_strokes and it < 6:  # 降密度
        step_min *= 1.6
        strokes = build(step_min)
        it += 1
    if len(strokes) > max_strokes:
        strokes.sort(key=_poly_len, reverse=True)
        strokes = strokes[:max_strokes]
    return strokes


# ---------------------------------------------------------------- 位图：map 区域排线+轮廓

def _fill_regions(cluster_masks):
    """色簇 mask → 填充区域（label + 填充率 ≥0.35）。
    返回 [(region_mask, hue, angle_deg, step_mm), ...]。
    """
    from scipy.ndimage import label as _ndlabel
    hatch_table = [
        (45.0, 1.6), (135.0, 1.6), (0.0, 1.2),
        (90.0, 1.2), (45.0, 2.4), (135.0, 2.4),
    ]
    regions = []
    for mask, hue, _area in cluster_masks:
        labeled, n = _ndlabel(mask)
        for comp_id in range(1, n + 1):
            comp = labeled == comp_id
            comp_area = int(comp.sum())
            if comp_area == 0:
                continue
            ys, xs = np.nonzero(comp)
            bw = int(xs.max() - xs.min() + 1)
            bh = int(ys.max() - ys.min() + 1)
            fill_ratio = comp_area / max(bw * bh, 1)
            # 图例小块(<800px²)不作填充区，交给 legend_blocks 另处理
            if fill_ratio >= 0.35 and comp_area >= 800:
                angle, step = hatch_table[len(regions) % len(hatch_table)]
                regions.append((comp, hue, angle, step))
    return regions


def _hatch_region(mask, angle_deg, step_mm, s, ox, oy, rng):
    """平行排线填充区域：沿方向采样每 0.4mm，保留≥0.8mm 的在内段。"""
    h, w = mask.shape
    theta = math.radians(angle_deg)
    ct, st_ = math.cos(theta), math.sin(theta)
    px_, py_ = -st_, ct

    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return []

    x0_mm = ox + xs.min() * s
    x1_mm = ox + (xs.max() + 1) * s
    y0_mm = oy + ys.min() * s
    y1_mm = oy + (ys.max() + 1) * s
    corners = [(x0_mm, y0_mm), (x1_mm, y0_mm), (x0_mm, y1_mm), (x1_mm, y1_mm)]
    perp_vals = [cx * px_ + cy * py_ for cx, cy in corners]
    s_min = min(perp_vals) - step_mm
    s_max = max(perp_vals) + step_mm
    dir_vals = [cx * ct + cy * st_ for cx, cy in corners]
    t_min = min(dir_vals) - 1.0
    t_max = max(dir_vals) + 1.0

    out = []
    sample_step = 0.4  # mm
    n_lines = int((s_max - s_min) / step_mm) + 1
    for k in range(n_lines):
        s_val = s_min + k * step_mm
        n_samples = int((t_max - t_min) / sample_step) + 1
        in_mask = []
        for j in range(n_samples):
            t = t_min + j * sample_step
            x = t * ct + s_val * px_
            y = t * st_ + s_val * py_
            pxi = int((x - ox) / s)
            pyi = int((y - oy) / s)
            if 0 <= pxi < w and 0 <= pyi < h and mask[pyi, pxi]:
                in_mask.append((t, x, y))
        if not in_mask:
            continue
        run_start = 0
        for j in range(1, len(in_mask) + 1):
            if j == len(in_mask) or in_mask[j][0] - in_mask[j - 1][0] > sample_step * 1.5:
                run = in_mask[run_start:j]
                if len(run) >= 2:
                    run_len = math.dist((run[0][1], run[0][2]), (run[-1][1], run[-1][2]))
                    if run_len >= 0.8:
                        pts = []
                        for _t, x, y in run:
                            jx = float(rng.uniform(-0.1, 0.1))
                            jy = float(rng.uniform(-0.1, 0.1))
                            pts.append((x + jx, y + jy))
                        if len(pts) >= 2:
                            out.append(pts)
                run_start = j
    return out


def _map_strokes(rgb: np.ndarray, gray: np.ndarray,
                 box_w: float, box_h: float, max_strokes: int):
    """map → 区域排线 + 边界轮廓。"""
    from scipy.ndimage import binary_erosion, label as _ndlabel

    h, w = gray.shape
    s = min(box_w / w, box_h / h) if w > 0 and h > 0 else 0.0
    if s <= 0:
        return []
    ox = (box_w - w * s) / 2.0
    oy = (box_h - h * s) / 2.0

    clusters, black_mask, ink_total = _cluster_series(rgb)
    regions = _fill_regions(clusters)

    def trace_box(mask):
        res = []
        for pts in _trace_ink(mask):
            if len(pts) < 2:
                continue
            mp = _dedup(_px_to_box(pts, w, h, box_w, box_h))
            mp = _clamp_pts(_rdp(mp, RDP_TOL), box_w, box_h)
            if len(mp) >= 2:
                res.append(mp)
        return res

    # 边界（形态学梯度，始终保留）
    outline_strokes = []
    for mask, _hue, _area in clusters + [(black_mask, -1.0, int(black_mask.sum()))]:
        if not mask.any():
            continue
        eroded = binary_erosion(mask, iterations=1)
        boundary = mask & ~eroded
        outline_strokes.extend(trace_box(boundary))

    # 图例小块→ 3-5 条短排线 + 轮廓框
    legend_strokes = []
    for mask, hue, _area in clusters:
        if hue < 0:
            continue
        labeled, n = _ndlabel(mask)
        for comp_id in range(1, n + 1):
            comp = labeled == comp_id
            comp_area = int(comp.sum())
            if comp_area < 20 or comp_area > 800:
                continue
            ys, xs = np.nonzero(comp)
            cx_mm = ox + (float(xs.mean()) + 0.5) * s
            cy_mm = oy + (float(ys.mean()) + 0.5) * s
            w_mm, h_mm = 8.0, 4.0
            x0 = min(max(cx_mm - w_mm / 2, 0.0), max(box_w - w_mm, 0.0))
            y0 = min(max(cy_mm - h_mm / 2, 0.0), max(box_h - h_mm, 0.0))
            legend_strokes.append([(x0, y0), (x0 + w_mm, y0), (x0 + w_mm, y0 + h_mm),
                                   (x0, y0 + h_mm), (x0, y0)])
            # 3-5 条短排线
            rng_l = np.random.default_rng(42)
            for frac in (0.25, 0.45, 0.65, 0.85):
                ly = y0 + h_mm * frac
                legend_strokes.append([(x0 + 1.0, ly), (x0 + w_mm - 1.0, ly)])

    # 排线填充（预算内）
    rng = np.random.default_rng(42)
    hatch_strokes = []
    step_mult = 1.0
    for _round in range(6):
        hatch_strokes = []
        for region_mask, _hue, angle, step in regions:
            for pts in _hatch_region(region_mask, angle, step * step_mult, s, ox, oy, rng):
                mp = _dedup(_clamp_pts(pts, box_w, box_h))
                if len(mp) >= 2:
                    hatch_strokes.append(mp)
        total = len(outline_strokes) + len(legend_strokes) + len(hatch_strokes)
        if total <= max_strokes:
            break
        step_mult *= 1.5

    all_strokes = outline_strokes + legend_strokes + hatch_strokes
    if len(all_strokes) > max_strokes:
        all_strokes.sort(key=_poly_len, reverse=True)
        all_strokes = all_strokes[:max_strokes]
    return all_strokes
