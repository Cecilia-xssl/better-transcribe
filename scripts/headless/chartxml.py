"""chartxml.py —— Word/WPS 图表（word/charts/chart*.xml）→ 手绘笔画。

解析 c:chartSpace（DrawingML Chart），把 line / scatter / area / bar(含 stacked、
percentStacked) / pie / doughnut / radar（bubble 尽力）绘成手写风格折线，供写字机描摹。
坐标约定与 layout 一致：笔画为 mm 折线，图框左上为原点，y 向下；文字走写字字库
（模块内惰性导入 layout.draw_char / char_advance_mm / expand_chars，避免循环依赖）。
线型轮转与 img2stroke 图片版同规则：实线 / 虚线(3,1.5) / 点线(0.6,1.2) / 点划(3,1.2,0.5,1.2)，
虚线烘焙为独立短线段。标签均按本地名匹配（兼容 c: 前缀与默认命名空间）。
解析/绘制异常返回 0 不抛。
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

# 线型烘焙（mm，与 img2stroke._LINE_STYLE_PATTERNS 同值）
_LINE_STYLE_PATTERNS = {
    "dash": [(3.0, 1.5)],
    "dot": [(0.6, 1.2)],
    "dashdot": [(3.0, 1.2), (0.5, 1.2)],
}
_STYLE_CYCLE = ["solid", "dash", "dot", "dashdot"]
# 排线纹理（系列/扇区轮转角度），线距 mm
_HATCH_ANGLES = [0.0, 45.0, 90.0, 135.0]
_HATCH_SPACING = 2.0
# 散点标记轮转
_MARKER_CYCLE = ["filled", "circle", "cross", "plus"]
_MARKER_SIZE = 1.2  # mm

# 文本竖排占位（em 倍数，覆盖 gfont 字身 y∈[-450,+21]/470）
_TEXT_ASC = 0.96
_TEXT_DESC = 0.06

_MIN_FONT = 1.8  # 字号下限 mm


# ---------------------------------------------------------------- XML 基础（按本地名匹配）

def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    if ":" in tag:
        return tag.rsplit(":", 1)[1]
    return tag


def _kids(el, name: str):
    return [c for c in el if _local(c.tag) == name]


def _kid(el, name: str):
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _desc(el, name: str):
    """首个本地名为 name 的后代（含自身）。"""
    if el is None:
        return None
    for d in el.iter():
        if _local(d.tag) == name:
            return d
    return None


def _attr(el, name: str, default: str | None = None) -> str | None:
    if el is None:
        return default
    v = el.get(name)
    if v is not None:
        return v
    for k, val in el.attrib.items():
        if _local(k) == name:
            return val
    return default


def _to_float(s) -> float | None:
    if s is None:
        return None
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 数值格式

def _fmt_num(v) -> str:
    """保留一位小数并去尾 0。"""
    f = _to_float(v)
    if f is None:
        return ""
    if abs(f) < 0.05:
        f = 0.0
    s = f"{f:.1f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _fmt_pct(frac: float) -> str:
    return _fmt_num(frac * 100.0) + "%"


# ---------------------------------------------------------------- 文本（惰性导入 layout）

def _text_w(ctx, text: str, size: float) -> float:
    from layout import char_advance_mm, expand_chars
    return sum(char_advance_mm(ctx, c, size) for c in expand_chars(text))


def _draw_text(ctx, text: str, x: float, baseline: float, size: float,
               align: str = "left") -> float:
    """写一行字，返回宽度 mm。align: left/center/right。"""
    from layout import draw_char, char_advance_mm, expand_chars
    text = expand_chars(text)
    w = sum(char_advance_mm(ctx, c, size) for c in text)
    if align == "center":
        x -= w / 2.0
    elif align == "right":
        x -= w
    xx = x
    for c in text:
        xx += draw_char(ctx, c, xx, baseline, size)
    return w


def _draw_text_rot(ctx, text: str, cx: float, cy: float, size: float,
                   angle_deg: float) -> float:
    """绕 (cx,cy) 旋转 angle_deg 写一行（居中对齐），用于轴标题竖排。"""
    from layout import draw_char, char_advance_mm, expand_chars
    text = expand_chars(text)
    w = sum(char_advance_mm(ctx, c, size) for c in text)
    n0 = len(ctx.strokes)
    xx = cx - w / 2.0
    for c in text:
        xx += draw_char(ctx, c, xx, cy, size)
    if abs(angle_deg) > 1e-9 and len(ctx.strokes) > n0:
        a = math.radians(angle_deg)
        ca, sa = math.cos(a), math.sin(a)
        for st in ctx.strokes[n0:]:
            st.points = [(cx + (px - cx) * ca - (py - cy) * sa,
                          cy + (px - cx) * sa + (py - cy) * ca)
                         for px, py in st.points]
    return w


# ---------------------------------------------------------------- 笔画追加

def _add(ctx, pts) -> None:
    from layout import Stroke
    pts = list(pts)
    if len(pts) < 2:
        return
    # 去掉完全重合的连续点
    clean = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - clean[-1][0]) > 1e-9 or abs(p[1] - clean[-1][1]) > 1e-9:
            clean.append(p)
    if len(clean) >= 2:
        ctx.strokes.append(Stroke(clean))


def _add_closed(ctx, pts) -> None:
    pts = list(pts)
    if len(pts) >= 3:
        _add(ctx, pts + [pts[0]])
    else:
        _add(ctx, pts)


# ---------------------------------------------------------------- 线型烘焙（同 img2stroke 规则）

def _extract_segment(pts, cum, start, end):
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


def _line_style_split(pts, style_name: str):
    """按线型把折线切成多条实线段笔画（gap 处抬笔）。"""
    if style_name == "solid" or len(pts) < 2:
        return [list(pts)]
    pattern = _LINE_STYLE_PATTERNS.get(style_name)
    if not pattern:
        return [list(pts)]
    cum = [0.0]
    for i in range(len(pts) - 1):
        cum.append(cum[-1] + math.dist(pts[i], pts[i + 1]))
    total = cum[-1]
    if total <= 1e-9:
        return [list(pts)]
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
    return out if out else [list(pts)]


# ---------------------------------------------------------------- 几何工具

def _hatch(poly, angle_deg: float, spacing: float, phase: float = 0.0):
    """多边形内 45° 系平行排线 → 线段列表 [((x,y),(x,y)), ...]（偶奇规则配对）。"""
    if len(poly) < 3 or spacing <= 0:
        return []
    a = math.radians(angle_deg)
    ca, sa = math.cos(-a), math.sin(-a)
    rp = [(px * ca - py * sa, px * sa + py * ca) for px, py in poly]
    ys = [p[1] for p in rp]
    y_min, y_max = min(ys), max(ys)
    segs = []
    k = math.floor((y_min - phase) / spacing)
    while True:
        yy = phase + k * spacing
        k += 1
        if yy > y_max + 1e-9:
            break
        if yy < y_min - 1e-9:
            continue
        xs = []
        n = len(rp)
        for i in range(n):
            x1, y1 = rp[i]
            x2, y2 = rp[(i + 1) % n]
            if (y1 < yy <= y2) or (y2 < yy <= y1):
                t = (yy - y1) / (y2 - y1)
                xs.append(x1 + t * (x2 - x1))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            if xs[i + 1] - xs[i] > 1e-9:
                segs.append(((xs[i], yy), (xs[i + 1], yy)))
    if not segs:
        return []
    ca, sa = math.cos(a), math.sin(a)
    out = []
    for p0, p1 in segs:
        q0 = (p0[0] * ca - p0[1] * sa, p0[0] * sa + p0[1] * ca)
        q1 = (p1[0] * ca - p1[1] * sa, p1[0] * sa + p1[1] * ca)
        out.append((q0, q1))
    return out


def _arc_pts(cx: float, cy: float, r: float, a0: float, a1: float, seg: int | None = None):
    """圆弧折线（弧度，屏幕系 y 向下）。"""
    span = abs(a1 - a0)
    if seg is None:
        seg = max(2, int(span / (math.pi / 36.0)) + 1)
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / seg),
             cy + r * math.sin(a0 + (a1 - a0) * i / seg)) for i in range(seg + 1)]


def _circle_pts(cx: float, cy: float, r: float, n: int = 12):
    return [(cx + r * math.cos(2 * math.pi * i / n),
             cy + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


def _draw_marker(ctx, kind: str, cx: float, cy: float, size: float) -> None:
    """散点/折线数据点标记：filled/circle/cross/plus。"""
    r = size / 2.0
    if kind == "filled":
        _add_closed(ctx, _circle_pts(cx, cy, r, 10))
        # 3 条弦线示意实心
        _add(ctx, [(cx - r * 0.8, cy), (cx + r * 0.8, cy)])
        _add(ctx, [(cx - r * 0.5, cy - r * 0.6), (cx + r * 0.5, cy + r * 0.6)])
        _add(ctx, [(cx - r * 0.5, cy + r * 0.6), (cx + r * 0.5, cy - r * 0.6)])
    elif kind == "circle":
        _add_closed(ctx, _circle_pts(cx, cy, r, 10))
    elif kind == "cross":
        _add(ctx, [(cx - r, cy - r), (cx + r, cy + r)])
        _add(ctx, [(cx - r, cy + r), (cx + r, cy - r)])
    else:  # plus
        _add(ctx, [(cx - r, cy), (cx + r, cy)])
        _add(ctx, [(cx, cy - r), (cx, cy + r)])


def _sector_poly(cx, cy, r_in, r_out, a0, a1):
    """扇形/圆环扇形闭合多边形（屏幕系角度，y 向下）。"""
    pts = _arc_pts(cx, cy, r_out, a0, a1)
    if r_in > 1e-9:
        pts += _arc_pts(cx, cy, r_in, a1, a0)
    else:
        pts.append((cx, cy))
    return pts


# ---------------------------------------------------------------- 刻度计算

def _nice_ticks(vmin: float, vmax: float, target: int = 5) -> list[float]:
    """把 [vmin, vmax] 扩成 4~6 格的整刻度列表（含端点外扩到整步长）。"""
    if not (math.isfinite(vmin) and math.isfinite(vmax)):
        return [0.0, 1.0]
    if vmax - vmin < 1e-9:
        vmax = vmin + 1.0
    raw = (vmax - vmin) / max(1, target)
    mag = 10.0 ** math.floor(math.log10(raw))
    step = mag
    for m in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= m * mag:
            step = m * mag
            break
    lo = math.floor(vmin / step) * step
    hi = math.ceil(vmax / step) * step
    ticks = []
    t = lo
    n_guard = 0
    while t <= hi + step * 1e-6 and n_guard < 64:
        ticks.append(round(t, 10))
        t += step
        n_guard += 1
    return ticks if len(ticks) >= 2 else [vmin, vmax]


# ---------------------------------------------------------------- 图表数据提取

def _cache_map(container):
    """c:cat/c:val/c:xVal/c:tx/c:size 容器 → {idx: 文本}（multiLvlStrRef 优雅退化）。"""
    out: dict[int, str] = {}
    order = 0
    if container is None:
        return out
    for pt in container.iter():
        if _local(pt.tag) != "pt":
            continue
        idx_s = _attr(pt, "idx")
        idx = int(float(idx_s)) if idx_s not in (None, "") else order
        order = idx + 1
        v_el = _kid(pt, "v")
        out[idx] = (v_el.text or "") if v_el is not None else ""
    return out


def _cache_strs(container) -> list[str | None]:
    d = _cache_map(container)
    if not d:
        return []
    n = max(d) + 1
    return [d.get(i) for i in range(n)]


def _cache_nums(container) -> list[float | None]:
    return [_to_float(s) for s in _cache_strs(container)]


def _el_text(el) -> str:
    """c:title/c:tx/... → 拼接 c:v / a:t 文本。"""
    if el is None:
        return ""
    parts = []
    for d in el.iter():
        if _local(d.tag) in ("t", "v") and d.text:
            parts.append(d.text)
    return "".join(parts).strip()


def _series_name(ser) -> str:
    tx = _kid(ser, "tx")
    if tx is None:
        return ""
    lit = _kid(tx, "v")
    if lit is not None and lit.text:
        return lit.text.strip()
    vals = _cache_strs(tx)
    for v in vals:
        if v:
            return str(v).strip()
    return ""


def _extract_series(group) -> list[dict]:
    sers = []
    for i, ser in enumerate(_kids(group, "ser")):
        cat = _cache_strs(_kid(ser, "cat"))
        if not cat:
            cat = _cache_strs(_desc(ser, "multiLvlStrRef"))
        x = _cache_nums(_kid(ser, "xVal"))
        val = _cache_nums(_kid(ser, "val"))
        if not val:
            val = _cache_nums(_kid(ser, "yVal"))
        size = _cache_nums(_kid(ser, "size"))
        name = _series_name(ser) or f"系列{i + 1}"
        # 归一长度
        n = max([len(cat), len(x), len(val), len(size)], default=0)
        if n == 0:
            continue
        def fit(a, fill):
            return list(a) + [fill] * (n - len(a))
        sers.append({
            "name": name,
            "cat": fit(cat, None),
            "x": fit(x, None),
            "val": fit(val, None),
            "size": fit(size, None),
            "marker": _marker_of(ser),
            "dlabels": _has_dlbls(ser, group),
        })
    return sers


def _marker_of(ser) -> str:
    """c:marker/c:symbol → 'none' 或具体符号名；无 marker 元素 → ''（默认小点）。"""
    mk = _kid(ser, "marker")
    if mk is None:
        return ""
    sym = _attr(_kid(mk, "symbol"), "val")
    return (sym or "").lower()


def _has_dlbls(ser, group) -> bool:
    for el in (ser, group):
        for c in el:
            if _local(c.tag) == "dLbls":
                if (_attr(_kid(c, "delete"), "val") or "").lower() in ("1", "true"):
                    return False
                return True
    return False


def _axis_info(ax) -> dict:
    sc = _kid(ax, "scaling")
    vmin = vmax = None
    orient = "minMax"
    if sc is not None:
        vmin = _to_float(_attr(_kid(sc, "min"), "val"))
        vmax = _to_float(_attr(_kid(sc, "max"), "val"))
        orient = (_attr(_kid(sc, "orientation"), "val") or "minMax").lower()
    return {
        "title": _el_text(_kid(ax, "title")),
        "min": vmin,
        "max": vmax,
        "grid": _kid(ax, "majorGridlines") is not None,
        "tick": (_attr(_kid(ax, "tickLblPos"), "val") or "nextTo").lower(),
        "pos": (_attr(_kid(ax, "axPos"), "val") or "").lower(),
        "orient": orient,
        "id": _attr(_kid(ax, "axId"), "val") or "",
        "deleted": (_attr(_kid(ax, "delete"), "val") or "").lower() in ("1", "true"),
    }


def _group_ax_ids(group) -> list[str]:
    return [(_attr(c, "val") or "") for c in group if _local(c.tag) == "axId"]


def _find_axis(plot_area, ax_id: str):
    if not ax_id:
        return None
    for c in plot_area:
        if _local(c.tag) in ("catAx", "valAx", "dateAx", "serAx"):
            if (_attr(_kid(c, "axId"), "val") or "") == ax_id:
                return c
    return None


def _chart_title_text(chart_el) -> str:
    t = _kid(chart_el, "title")
    return _el_text(t)


def _legend_pos(chart_el) -> str | None:
    lg = _kid(chart_el, "legend")
    if lg is None:
        return None
    if (_attr(_kid(lg, "delete"), "val") or "").lower() in ("1", "true"):
        return None
    return (_attr(_kid(lg, "legendPos"), "val") or "r").lower()


def _doughnut_hole(group) -> float:
    hs = _kid(group, "holeSize")
    v = _to_float(_attr(hs, "val")) if hs is not None else None
    if v is None:
        return 0.5
    return min(0.9, max(0.1, v / 100.0))


def _first_slice_deg(group) -> float:
    el = _kid(group, "firstSliceAng")
    v = _to_float(_attr(el, "val")) if el is not None else None
    return v if v is not None else 0.0


# ---------------------------------------------------------------- 图例 / 标题

def _draw_swatch(ctx, entry: dict, x0: float, y0: float, w: float, h: float) -> None:
    """图例小样：线型短线 / 排线方块 / 标记 / 圆。"""
    kind = entry.get("sw", "line")
    if kind == "line":
        pts = [(x0, y0 + h / 2.0), (x0 + w, y0 + h / 2.0)]
        for seg in _line_style_split(pts, entry.get("style", "solid")):
            _add(ctx, seg)
    elif kind == "hatch":
        poly = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
        _add_closed(ctx, poly)
        for p0, p1 in _hatch(poly, entry.get("angle", 45.0), _HATCH_SPACING,
                             entry.get("phase", 0.0)):
            _add(ctx, [p0, p1])
    elif kind == "marker":
        _draw_marker(ctx, entry.get("marker", "circle"), x0 + w / 2.0, y0 + h / 2.0,
                     min(w, h) * 0.8)
    else:  # circle（bubble）
        _add_closed(ctx, _circle_pts(x0 + w / 2.0, y0 + h / 2.0, min(w, h) * 0.45, 10))


def _legend_entries(kind: str, series: list[dict], pie_names: list[str] | None = None):
    """图例条目（饼/环按扇区，其余按系列）。"""
    entries = []
    if kind in ("pie", "doughnut"):
        for i, nm in enumerate(pie_names or []):
            entries.append({"text": nm, "sw": "hatch",
                            "angle": _HATCH_ANGLES[i % 4],
                            "phase": i * 0.66})
        return entries
    for i, s in enumerate(series):
        st = _STYLE_CYCLE[i % 4]
        if kind == "bar":
            e = {"text": s["name"], "sw": "hatch", "angle": _HATCH_ANGLES[i % 4],
                 "phase": i * 0.66}
        elif kind == "scatter":
            e = {"text": s["name"], "sw": "marker", "marker": _MARKER_CYCLE[i % 4]}
        elif kind == "bubble":
            e = {"text": s["name"], "sw": "circle"}
        elif kind == "area":
            e = {"text": s["name"], "sw": "hatch", "angle": 45.0, "phase": i * 0.66}
        else:  # line / radar
            e = {"text": s["name"], "sw": "line", "style": st}
        entries.append(e)
    return entries


def _legend_size(ctx, entries, size: float, pos: str):
    """图例占位 (w, h)；l/r 竖排，t/b 横排换行。"""
    sw = max(3.5, size * 1.2)
    pad = size * 0.6
    if pos in ("l", "r"):
        widths = [sw + pad + _text_w(ctx, e["text"], size) for e in entries]
        w = (max(widths) if widths else 0.0) + size
        row_h = max(size * 1.7, sw + size * 0.5)
        return w, row_h * max(1, len(entries))
    row_h = max(size * 1.7, sw + size * 0.5)
    widths = [sw + pad + _text_w(ctx, e["text"], size) + pad for e in entries]
    total = sum(widths)
    return total, row_h


def _draw_legend(ctx, entries, pos: str, x: float, y: float, size: float,
                 max_span: float) -> tuple[float, float]:
    """画图例，返回占用 (w, h)。l/r 竖排；t/b 横排换行（宽超过 max_span 折行）。"""
    if not entries:
        return 0.0, 0.0
    sw = max(3.5, size * 1.2)
    pad = size * 0.6
    row_h = max(size * 1.7, sw + size * 0.5)
    if pos in ("l", "r"):
        yy = y
        maxw = 0.0
        for e in entries:
            tw = _text_w(ctx, e["text"], size)
            _draw_swatch(ctx, e, x, yy + (row_h - sw) / 2.0, sw, sw)
            _draw_text(ctx, e["text"], x + sw + pad, yy + row_h * 0.5 + size * 0.36,
                       size)
            maxw = max(maxw, sw + pad + tw)
            yy += row_h
        return maxw, yy - y
    # 横排换行
    xx, yy = x, y
    row_w = 0.0
    for e in entries:
        tw = _text_w(ctx, e["text"], size)
        ew = sw + pad + tw + pad
        if xx > x and xx + ew > x + max_span:
            xx = x
            yy += row_h
            row_w = max(row_w, 0.0)
        _draw_swatch(ctx, e, xx, yy + (row_h - sw) / 2.0, sw, sw)
        _draw_text(ctx, e["text"], xx + sw + pad, yy + row_h * 0.5 + size * 0.36,
                   size)
        xx += ew
        row_w = max(row_w, xx - x)
    return row_w, yy + row_h - y


# ---------------------------------------------------------------- 笛卡尔图（line/area/bar）

def _render_cat_chart(ctx, kind: str, group, sers: list[dict], ncat: int,
                      x: float, y: float, w: float, h: float,
                      title: str, legend_pos: str | None,
                      ax_cat: dict | None, ax_val: dict | None) -> None:
    bar_dir = (_attr(_kid(group, "barDir"), "val") or "col").lower() if kind == "bar" else "col"
    grouping = (_attr(_kid(group, "grouping"), "val") or "clustered").lower() if kind == "bar" else "normal"
    horiz = kind == "bar" and bar_dir == "bar"
    stacked = grouping in ("stacked", "percentstacked")
    pct = grouping == "percentstacked"

    ax_cat = ax_cat or {}
    ax_val = ax_val or {}

    # ---- 字号
    size_title = max(_MIN_FONT, min(h * 0.07, 20.0))
    size_tick = max(_MIN_FONT, min(h * 0.05, 14.0) * 0.9)
    size_cat = size_tick

    pad = max(0.8, min(w, h) * 0.015)

    # ---- 数值范围与刻度
    if pct:
        v_lo, v_hi = 0.0, 1.0
        ticks = [0.0, 0.25, 0.5, 0.75, 1.0]
        fmt = _fmt_pct
    else:
        vals = []
        for s in sers:
            if stacked:
                for i in range(ncat):
                    acc = 0.0
                    for ss in sers:
                        v = ss["val"][i] if i < len(ss["val"]) else None
                        if v is not None and math.isfinite(v):
                            acc += v
                    vals.append(acc)
                break
            for v in s["val"]:
                if v is not None and math.isfinite(v):
                    vals.append(v)
        if not vals:
            return
        v_lo, v_hi = min(vals), max(vals)
        force_zero = kind in ("bar", "area")
        if force_zero:
            v_lo = min(v_lo, 0.0)
            v_hi = max(v_hi, 0.0)
        ticks = _nice_ticks(v_lo, v_hi, 5)
        v_lo, v_hi = ticks[0], ticks[-1]
        ov_min, ov_max = ax_val.get("min"), ax_val.get("max")
        if ov_min is not None:
            v_lo = ov_min
        if ov_max is not None:
            v_hi = ov_max
        if v_hi - v_lo < 1e-9:
            v_hi = v_lo + 1.0
        # 按最终范围重算整刻度
        ticks = [t for t in _nice_ticks(v_lo, v_hi, 5) if v_lo - 1e-9 <= t <= v_hi + 1e-9]
        if len(ticks) < 2:
            ticks = [v_lo, v_hi]
        fmt = _fmt_num

    rev = (ax_val.get("orient") == "maxmin")

    # ---- 文本占位测量
    tick_strs = [fmt(t) for t in ticks]
    tick_w = max((_text_w(ctx, s, size_tick) for s in tick_strs), default=0.0)
    tick_h = size_tick * 1.25

    # 类别标签（必要时隔个丢弃）
    cat_labels = []
    for i in range(ncat):
        lab = ""
        for s in sers:
            if i < len(s["cat"]) and s["cat"][i] not in (None, ""):
                lab = str(s["cat"][i])
                break
        cat_labels.append(lab)
    cat_w_max = max((_text_w(ctx, s, size_cat) for s in cat_labels), default=0.0)

    val_title = ax_val.get("title") or ""
    cat_title = ax_cat.get("title") or ""
    val_show = ax_val.get("tick", "nextto") != "none" and not ax_val.get("deleted")
    cat_show = ax_cat.get("tick", "nextto") != "none" and not ax_cat.get("deleted")

    # ---- 区域分配
    top = y + pad
    if title:
        _draw_text(ctx, title, x + w / 2.0, top + size_title * _TEXT_ASC, size_title,
                   align="center")
        top += size_title * 1.45 + pad

    # 图例（t 先占位）
    ent = _legend_entries(kind, sers)
    lg_w = lg_h = 0.0
    lg_rows = 1
    if legend_pos == "t" and ent:
        _, lg_h = _legend_size(ctx, ent, size_tick, "t")
        top += lg_h + pad

    bottom = y + h - pad
    if legend_pos == "b" and ent:
        _, lgh = _legend_size(ctx, ent, size_tick, "b")
        bottom -= lgh + pad

    if horiz:
        left = x + pad + (size_tick * 1.6 if cat_title else 0.0)
        if cat_show:
            left += min(cat_w_max, w * 0.30) + 2.0
        right = x + w - pad
        if legend_pos == "r" and ent:
            lg_w, _ = _legend_size(ctx, ent, size_tick, "r")
            right -= lg_w + pad
        elif legend_pos == "l" and ent:
            lg_w, _ = _legend_size(ctx, ent, size_tick, "l")
            left += lg_w + pad
        bot_res = ((2.6 + 1.15 * size_tick) if val_show else 0.0) + (
            size_tick * 1.5 if val_title else 0.0)
        plot_x, plot_w = left, max(1.0, right - left)
        plot_y = top
        plot_h = max(1.0, bottom - bot_res - top)
    else:
        left = x + pad
        if val_show:
            left += tick_w + 2.5
        if val_title:
            left += size_tick * 1.7
        right = x + w - pad
        if legend_pos == "r" and ent:
            lg_w, _ = _legend_size(ctx, ent, size_tick, "r")
            right -= lg_w + pad
        elif legend_pos == "l" and ent:
            lg_w, _ = _legend_size(ctx, ent, size_tick, "l")
            left += lg_w + pad
        plot_x, plot_w = left, max(1.0, right - left)
        bot_res = ((1.2 + 1.15 * size_cat) if cat_show else 0.0) + (
            size_tick * 1.5 if cat_title else 0.0)
        plot_y = top
        plot_h = max(1.0, bottom - bot_res - top)

    px, py, pw, ph = plot_x, plot_y, plot_w, plot_h

    # ---- 值映射
    def val_to(v: float, a: float, span: float) -> float:
        t = (v - v_lo) / (v_hi - v_lo)
        if rev:
            t = 1.0 - t
        return a + t * span

    def vpos(v: float) -> float:
        # 纵向（值向上）
        t = (v - v_lo) / (v_hi - v_lo)
        if rev:
            t = 1.0 - t
        return py + ph - t * ph

    def hpos(v: float) -> float:
        t = (v - v_lo) / (v_hi - v_lo)
        if rev:
            t = 1.0 - t
        return px + t * pw

    pos_of = hpos if horiz else vpos

    # ---- 网格线
    grid = ax_val.get("grid", False)
    if grid:
        for t in ticks:
            c = pos_of(t)
            if horiz:
                _add(ctx, [(c, py), (c, py + ph)])
            else:
                _add(ctx, [(px, c), (px + pw, c)])

    # ---- 类别槽位
    if horiz:
        slot = ph / max(1, ncat)

        def slot_center(i):
            return py + (i + 0.5) * slot
    else:
        slot = pw / max(1, ncat)

        def slot_center(i):
            return px + (i + 0.5) * slot

    # ---- 数据
    if kind == "bar":
        gap_w = _to_float(_attr(_kid(group, "gapWidth"), "val"))
        gap_w = 150.0 if gap_w is None else max(0.0, gap_w)
        baseline = pos_of(0.0) if v_lo <= 0.0 <= v_hi else (py + ph if not horiz else px)
        for si, s in enumerate(sers):
            angle = _HATCH_ANGLES[si % 4]
            phase = si * 0.66
            if pct:
                totals = []
                for i in range(ncat):
                    tot = 0.0
                    for ss in sers:
                        v = ss["val"][i] if i < len(ss["val"]) else None
                        if v is not None and math.isfinite(v) and v > 0:
                            tot += v
                    totals.append(tot)
            if stacked or pct:
                b = slot / (1.0 + gap_w / 100.0)
            else:
                b = slot / (len(sers) + gap_w / 100.0)
            for i in range(ncat):
                v = s["val"][i] if i < len(s["val"]) else None
                if v is None or not math.isfinite(v):
                    continue
                if pct:
                    tot = totals[i]
                    v = (v / tot) if tot > 1e-12 else 0.0
                if stacked or pct:
                    lo = 0.0
                    for ss in sers[:si]:
                        vv = ss["val"][i] if i < len(ss["val"]) else None
                        if vv is not None and math.isfinite(vv):
                            lo += (vv / totals[i]) if (pct and totals[i] > 1e-12) else vv
                    hi = lo + v
                    c0, c1 = pos_of(lo), pos_of(hi)
                else:
                    c0, c1 = baseline, pos_of(v)
                ctr = slot_center(i)
                if not (stacked or pct):
                    # 簇内并排：整体簇宽 b*系列数，按系列序错开
                    ctr = ctr - (b * len(sers)) / 2.0 + (si + 0.5) * b
                if horiz:
                    y0, y1 = ctr - b / 2.0, ctr + b / 2.0
                    x0, x1 = min(c0, c1), max(c0, c1)
                    rect = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                else:
                    x0, x1 = ctr - b / 2.0, ctr + b / 2.0
                    y0, y1 = min(c0, c1), max(c0, c1)
                    rect = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                _add_closed(ctx, rect)
                for p0, p1 in _hatch(rect, angle, _HATCH_SPACING, phase):
                    _add(ctx, [p0, p1])
                if s.get("dlabels"):
                    lab = _fmt_pct(v) if pct else _fmt_num(v)
                    lsize = size_tick
                    if _text_w(ctx, lab, lsize) > b * 1.15:
                        lsize = _MIN_FONT
                    if _text_w(ctx, lab, lsize) > b * 1.45:
                        lsize = 0.0  # 放不下则丢弃
                    if lsize > 0:
                        if horiz:
                            _draw_text(ctx, lab, x1 + 1.0, ctr + lsize * 0.36, lsize)
                        else:
                            _draw_text(ctx, lab, ctr, y0 - 0.6, lsize, align="center")
    elif kind == "area":
        # 基线：0 或绘图区底
        base_v = 0.0 if v_lo <= 0.0 <= v_hi else (v_lo if v_lo > 0 else v_hi)
        base_y = vpos(base_v)
        cum = [0.0] * ncat
        for si, s in enumerate(sers):
            top_pts = []
            for i in range(ncat):
                v = s["val"][i] if i < len(s["val"]) else None
                if v is None or not math.isfinite(v):
                    v = 0.0
                if stacked:
                    lo = cum[i]
                    hi = lo + v
                    cum[i] = hi
                else:
                    lo = base_v
                    hi = v
                top_pts.append((slot_center(i), vpos(hi), vpos(lo)))
            # 折线（上缘）+ 填充
            runs = []
            cur = []
            for (cx_, yhi, ylo) in top_pts:
                cur.append((cx_, yhi, ylo))
            poly_hi = [(p[0], p[1]) for p in cur]
            poly_lo = [(p[0], p[2]) for p in cur]
            if len(poly_hi) >= 2:
                poly = poly_hi + poly_lo[::-1]
                for p0, p1 in _hatch(poly, 45.0, _HATCH_SPACING, si * 0.66):
                    _add(ctx, [p0, p1])
                style = _STYLE_CYCLE[si % 4]
                for seg in _line_style_split(poly_hi, style):
                    _add(ctx, seg)
                _add(ctx, [poly_lo[0], poly_lo[-1]])
    else:  # line
        for si, s in enumerate(sers):
            pts = [(slot_center(i), vpos(v))
                   for i, v in enumerate(s["val"])
                   if i < ncat and v is not None and math.isfinite(v)]
            style = _STYLE_CYCLE[si % 4]
            run = []
            runs = []
            for i in range(ncat):
                v = s["val"][i] if i < len(s["val"]) else None
                if v is None or not math.isfinite(v):
                    if len(run) >= 2:
                        runs.append(run)
                    run = []
                    continue
                run.append((slot_center(i), vpos(v)))
            if len(run) >= 2:
                runs.append(run)
            elif len(run) == 1:
                runs.append(run)
            for r in runs:
                if len(r) >= 2:
                    for seg in _line_style_split(r, style):
                        _add(ctx, seg)
            if s.get("marker") != "none":
                for (mx, my) in pts:
                    _add_closed(ctx, _circle_pts(mx, my, 0.35, 8))

    # ---- 轴线与刻度
    if horiz:
        # 值 0 处竖线为类别轴；数值轴横线贴绘图区底边
        base_c = min(max(hpos(0.0) if v_lo <= 0.0 <= v_hi else px, px), px + pw)
        if not ax_cat.get("deleted"):
            _add(ctx, [(base_c, py), (base_c, py + ph)])
        if not ax_val.get("deleted"):
            _add(ctx, [(px, py + ph), (px + pw, py + ph)])
            for t in ticks:
                c = hpos(t)
                _add(ctx, [(c, py + ph), (c, py + ph + 2.0)])
                if val_show:
                    _draw_text(ctx, fmt(t), c, py + ph + 2.6 + size_tick * _TEXT_ASC,
                               size_tick, align="center")
        if val_title:
            _draw_text(ctx, val_title, px + pw / 2.0,
                       py + ph + 2.6 + size_tick * 1.25 + size_tick * _TEXT_ASC + 0.4,
                       size_tick, align="center")
        if cat_show:
            avail = px - x - pad - (size_tick * 1.6 if cat_title else 0.0) - 2.0
            step = 1
            if cat_w_max > avail:
                step = max(2, int(math.ceil(cat_w_max / max(1.0, avail))))
            for i in range(0, ncat, step):
                lab = cat_labels[i]
                if not lab:
                    continue
                _draw_text(ctx, lab, px - 2.0, slot_center(i) + size_cat * 0.36,
                           size_cat, align="right")
        if cat_title:
            _draw_text_rot(ctx, cat_title,
                           x + pad + size_tick * 0.8, py + ph / 2.0,
                           size_tick, -90.0)
    else:
        axis_c = pos_of(0.0) if v_lo <= 0.0 <= v_hi else py + ph
        if not ax_val.get("deleted"):
            _add(ctx, [(px, py), (px, py + ph)])
            for t in ticks:
                c = vpos(t)
                _add(ctx, [(px - 2.0, c), (px, c)])
                if val_show:
                    _draw_text(ctx, fmt(t), px - 2.5, c + size_tick * 0.36,
                               size_tick, align="right")
        if val_title:
            _draw_text_rot(ctx, val_title,
                           x + pad + size_tick * 0.85, py + ph / 2.0,
                           size_tick, -90.0)
        if not ax_cat.get("deleted"):
            _add(ctx, [(px, axis_c), (px + pw, axis_c)])
        if cat_show:
            step = 1
            slot_w = pw / max(1, ncat)
            if cat_w_max > slot_w * 0.95:
                step = max(2, int(math.ceil(cat_w_max / (slot_w * 0.95))))
            for i in range(0, ncat, step):
                lab = cat_labels[i]
                if not lab:
                    continue
                _draw_text(ctx, lab, slot_center(i), py + ph + 1.2 + size_cat * _TEXT_ASC,
                           size_cat, align="center")
        if cat_title:
            _draw_text(ctx, cat_title, px + pw / 2.0,
                       py + ph + tick_h + size_tick * _TEXT_ASC + 0.6, size_tick,
                       align="center")

    # ---- 图例（l/r/b 实际绘制）
    if ent and legend_pos in ("l", "r"):
        lg_w, lg_h = _legend_size(ctx, ent, size_tick, legend_pos)
        lx = x + w - pad - lg_w if legend_pos == "r" else x + pad
        ly = (py + ph / 2.0 - lg_h / 2.0) if lg_h < ph else py
        _draw_legend(ctx, ent, legend_pos, lx, ly, size_tick, lg_w + 1.0)
    elif ent and legend_pos == "b":
        _, lgh = _legend_size(ctx, ent, size_tick, "b")
        _draw_legend(ctx, ent, "b", x + pad, bottom - lgh, size_tick, w - 2 * pad)


# ---------------------------------------------------------------- 散点 / 气泡

def _render_num_chart(ctx, kind: str, sers: list[dict],
                      x: float, y: float, w: float, h: float,
                      title: str, legend_pos: str | None,
                      ax_x: dict | None, ax_y: dict | None) -> None:
    ax_x = ax_x or {}
    ax_y = ax_y or {}
    size_title = max(_MIN_FONT, min(h * 0.07, 20.0))
    size_tick = max(_MIN_FONT, min(h * 0.05, 14.0) * 0.9)
    pad = max(0.8, min(w, h) * 0.015)

    pairs = []
    for s in sers:
        pts = []
        for xv, yv in zip(s["x"], s["val"]):
            if xv is not None and yv is not None and math.isfinite(xv) and math.isfinite(yv):
                pts.append((xv, yv))
        if pts:
            pairs.append(pts)
    if not pairs:
        return
    xs = [p[0] for pts in pairs for p in pts]
    ys = [p[1] for pts in pairs for p in pts]

    def rng(vals, ax, force_zero):
        lo, hi = min(vals), max(vals)
        if force_zero:
            lo, hi = min(lo, 0.0), max(hi, 0.0)
        ticks = _nice_ticks(lo, hi, 5)
        lo, hi = ticks[0], ticks[-1]
        if ax.get("min") is not None:
            lo = ax["min"]
        if ax.get("max") is not None:
            hi = ax["max"]
        if hi - lo < 1e-9:
            hi = lo + 1.0
        ticks = [t for t in _nice_ticks(lo, hi, 5) if lo - 1e-9 <= t <= hi + 1e-9] or [lo, hi]
        return lo, hi, ticks

    x_lo, x_hi, x_ticks = rng(xs, ax_x, False)
    y_lo, y_hi, y_ticks = rng(ys, ax_y, False)
    x_rev = ax_x.get("orient") == "maxmin"
    y_rev = ax_y.get("orient") == "maxmin"

    top = y + pad
    if title:
        _draw_text(ctx, title, x + w / 2.0, top + size_title * _TEXT_ASC, size_title,
                   align="center")
        top += size_title * 1.45 + pad
    bottom = y + h - pad

    ent = _legend_entries(kind, sers)
    if legend_pos == "t" and ent:
        _, lg_h = _legend_size(ctx, ent, size_tick, "t")
        top += lg_h + pad
    if legend_pos == "b" and ent:
        _, lgh = _legend_size(ctx, ent, size_tick, "b")
        bottom -= lgh + pad

    y_strs = [_fmt_num(t) for t in y_ticks]
    y_lab_w = max((_text_w(ctx, s, size_tick) for s in y_strs), default=0.0)
    left = x + pad + y_lab_w + 2.5 + (size_tick * 1.7 if ax_y.get("title") else 0.0)
    right = x + w - pad
    if legend_pos == "r" and ent:
        lg_w, _ = _legend_size(ctx, ent, size_tick, "r")
        right -= lg_w + pad
    elif legend_pos == "l" and ent:
        lg_w, _ = _legend_size(ctx, ent, size_tick, "l")
        left += lg_w + pad
    bot_res = 2.6 + 1.15 * size_tick + (size_tick * 1.5 if ax_x.get("title") else 0.0)
    px, py = left, top
    pw, ph = max(1.0, right - left), max(1.0, bottom - bot_res - top)

    def X(v):
        t = (v - x_lo) / (x_hi - x_lo)
        if x_rev:
            t = 1.0 - t
        return px + t * pw

    def Y(v):
        t = (v - y_lo) / (y_hi - y_lo)
        if y_rev:
            t = 1.0 - t
        return py + ph - t * ph

    if ax_y.get("grid"):
        for t in y_ticks:
            _add(ctx, [(px, Y(t)), (px + pw, Y(t))])
    if ax_x.get("grid"):
        for t in x_ticks:
            _add(ctx, [(X(t), py), (X(t), py + ph)])

    # 轴线：过 0 点，否则贴边
    ax_y_c = X(0.0) if x_lo <= 0.0 <= x_hi else px
    ax_x_c = Y(0.0) if y_lo <= 0.0 <= y_hi else py + ph
    if not ax_y.get("deleted"):
        _add(ctx, [(ax_y_c, py), (ax_y_c, py + ph)])
    if not ax_x.get("deleted"):
        _add(ctx, [(px, ax_x_c), (px + pw, ax_x_c)])

    show_y = ax_y.get("tick", "nextto") != "none" and not ax_y.get("deleted")
    show_x = ax_x.get("tick", "nextto") != "none" and not ax_x.get("deleted")
    if show_y:
        for t in y_ticks:
            c = Y(t)
            _add(ctx, [(ax_y_c - 2.0, c), (ax_y_c, c)])
            _draw_text(ctx, _fmt_num(t), ax_y_c - 2.5, c + size_tick * 0.36,
                       size_tick, align="right")
    if show_x:
        for t in x_ticks:
            c = X(t)
            _add(ctx, [(c, ax_x_c), (c, ax_x_c + 2.0)])
            _draw_text(ctx, _fmt_num(t), c, ax_x_c + 2.6 + size_tick * _TEXT_ASC,
                       size_tick, align="center")
    if ax_y.get("title"):
        _draw_text_rot(ctx, ax_y["title"], x + pad + size_tick * 0.85, py + ph / 2.0,
                       size_tick, -90.0)
    if ax_x.get("title"):
        _draw_text(ctx, ax_x["title"], px + pw / 2.0,
                   ax_x_c + 2.6 + size_tick * 1.25 + size_tick * _TEXT_ASC + 0.4,
                   size_tick, align="center")

    if kind == "bubble":
        sizes = []
        for s in sers:
            for v in s["size"]:
                if v is not None and math.isfinite(v) and v > 0:
                    sizes.append(v)
        smax = max(sizes) if sizes else 1.0
        r_ref = min(pw, ph) * 0.09
        si = 0
        for s in sers:
            for i, (xv, yv) in enumerate(zip(s["x"], s["val"])):
                if xv is None or yv is None:
                    continue
                sv = s["size"][i] if i < len(s["size"]) else None
                r = r_ref
                if sv is not None and math.isfinite(sv) and sv > 0 and smax > 0:
                    r = r_ref * math.sqrt(sv / smax)
                _add_closed(ctx, _circle_pts(X(xv), Y(yv), max(0.8, r), 12))
            si += 1
    else:
        for si, pts in enumerate(pairs):
            mk = _MARKER_CYCLE[si % 4]
            for (xv, yv) in pts:
                _draw_marker(ctx, mk, X(xv), Y(yv), _MARKER_SIZE)

    if ent and legend_pos in ("l", "r"):
        lg_w, lg_h = _legend_size(ctx, ent, size_tick, legend_pos)
        lx = x + w - pad - lg_w if legend_pos == "r" else x + pad
        ly = py + ph / 2.0 - lg_h / 2.0
        _draw_legend(ctx, ent, legend_pos, lx, ly, size_tick, lg_w + 1.0)
    elif ent and legend_pos == "b":
        _, lgh = _legend_size(ctx, ent, size_tick, "b")
        _draw_legend(ctx, ent, "b", x + pad, bottom - lgh, size_tick, w - 2 * pad)


# ---------------------------------------------------------------- 饼 / 圆环

def _render_pie(ctx, kind: str, group, sers: list[dict],
                x: float, y: float, w: float, h: float,
                title: str, legend_pos: str | None) -> None:
    size_title = max(_MIN_FONT, min(h * 0.07, 20.0))
    size_tick = max(_MIN_FONT, min(h * 0.05, 14.0) * 0.9)
    pad = max(0.8, min(w, h) * 0.015)

    s = sers[0]
    vals = [v if (v is not None and math.isfinite(v) and v > 0) else 0.0 for v in s["val"]]
    total = sum(vals)
    if total <= 1e-12:
        return
    labels = []
    for i, v in enumerate(vals):
        nm = ""
        if i < len(s["cat"]) and s["cat"][i] not in (None, ""):
            nm = str(s["cat"][i])
        labels.append(nm or f"第{i + 1}项")
    pcts = [v / total for v in vals]

    top = y + pad
    if title:
        _draw_text(ctx, title, x + w / 2.0, top + size_title * _TEXT_ASC, size_title,
                   align="center")
        top += size_title * 1.45 + pad
    bottom = y + h - pad

    ent = _legend_entries("pie", sers, pie_names=labels)
    if legend_pos == "t" and ent:
        _, lg_h = _legend_size(ctx, ent, size_tick, "t")
        top += lg_h + pad
    if legend_pos == "b" and ent:
        _, lgh = _legend_size(ctx, ent, size_tick, "b")
        bottom -= lgh + pad

    left = x + pad
    right = x + w - pad
    if legend_pos == "r" and ent:
        lg_w, _ = _legend_size(ctx, ent, size_tick, "r")
        right -= lg_w + pad
    elif legend_pos == "l" and ent:
        lg_w, _ = _legend_size(ctx, ent, size_tick, "l")
        left += lg_w + pad

    # 引出标签占位：左右各留标签宽 + 引线
    lab_txt = [f"{labels[i]} {_fmt_pct(pcts[i])}" for i in range(len(vals))]
    lab_w = max((_text_w(ctx, t, size_tick) for t in lab_txt), default=0.0)
    lead = 4.0 + 3.0 + min(lab_w, w * 0.32)
    avail_w = max(10.0, (right - left) - 2.0 * lead)
    avail_h = max(10.0, bottom - top)
    cx = (left + right) / 2.0
    cy = top + avail_h / 2.0
    r_out = min(avail_w, avail_h) / 2.0 * 0.92
    r_in = r_out * _doughnut_hole(group) if kind == "doughnut" else 0.0

    a0_deg = _first_slice_deg(group)
    ang = math.radians(a0_deg)  # 0 = 12 点方向，顺时针
    for i, frac in enumerate(pcts):
        if frac <= 0:
            continue
        span = frac * 2.0 * math.pi
        # 屏幕系：12 点起顺时针 → 参数角 = ang - pi/2
        t0 = ang - math.pi / 2.0
        t1 = t0 + span
        poly = _sector_poly(cx, cy, r_in, r_out, t0, t1)
        _add_closed(ctx, poly)
        for p0, p1 in _hatch(poly, _HATCH_ANGLES[i % 4], _HATCH_SPACING, i * 0.66):
            _add(ctx, [p0, p1])
        # 引出标签
        mid = (t0 + t1) / 2.0
        p_arc = (cx + r_out * 1.02 * math.cos(mid), cy + r_out * 1.02 * math.sin(mid))
        p_elb = (cx + r_out * 1.16 * math.cos(mid), cy + r_out * 1.16 * math.sin(mid))
        side = 1.0 if math.cos(mid) >= 0 else -1.0
        p_end = (p_elb[0] + side * 3.2, p_elb[1])
        _add(ctx, [p_arc, p_elb, p_end])
        txt = lab_txt[i]
        tw = _text_w(ctx, txt, size_tick)
        if side > 0:
            tx = p_end[0] + 1.0
        else:
            tx = p_end[0] - 1.0 - tw
        tx = min(max(tx, x + 0.5), x + w - tw - 0.5)
        by = min(max(p_end[1] + size_tick * 0.36, y + size_tick * 0.7),
                 y + h - size_tick * 0.15)
        _draw_text(ctx, txt, tx, by, size_tick)
        ang += span

    if ent and legend_pos in ("l", "r"):
        lg_w, lg_h = _legend_size(ctx, ent, size_tick, legend_pos)
        lx = x + w - pad - lg_w if legend_pos == "r" else x + pad
        ly = cy - lg_h / 2.0
        _draw_legend(ctx, ent, legend_pos, lx, ly, size_tick, lg_w + 1.0)
    elif ent and legend_pos == "b":
        _, lgh = _legend_size(ctx, ent, size_tick, "b")
        _draw_legend(ctx, ent, "b", x + pad, bottom - lgh, size_tick, w - 2 * pad)


# ---------------------------------------------------------------- 雷达

def _render_radar(ctx, sers: list[dict], ncat: int,
                  x: float, y: float, w: float, h: float,
                  title: str, legend_pos: str | None,
                  ax_val: dict | None) -> None:
    ax_val = ax_val or {}
    size_title = max(_MIN_FONT, min(h * 0.07, 20.0))
    size_tick = max(_MIN_FONT, min(h * 0.05, 14.0) * 0.9)
    pad = max(0.8, min(w, h) * 0.015)

    vals = []
    for s in sers:
        for v in s["val"]:
            if v is not None and math.isfinite(v):
                vals.append(v)
    if not vals:
        return
    v_lo, v_hi = min(min(vals), 0.0), max(max(vals), 0.0)
    ticks = _nice_ticks(v_lo, v_hi, 4)
    v_lo, v_hi = ticks[0], ticks[-1]
    if ax_val.get("min") is not None:
        v_lo = ax_val["min"]
    if ax_val.get("max") is not None:
        v_hi = ax_val["max"]
    if v_hi - v_lo < 1e-9:
        v_hi = v_lo + 1.0
    ticks = [t for t in _nice_ticks(v_lo, v_hi, 4) if v_lo - 1e-9 <= t <= v_hi + 1e-9] or [v_lo, v_hi]

    top = y + pad
    if title:
        _draw_text(ctx, title, x + w / 2.0, top + size_title * _TEXT_ASC, size_title,
                   align="center")
        top += size_title * 1.45 + pad
    bottom = y + h - pad

    ent = _legend_entries("radar", sers)
    if legend_pos == "t" and ent:
        _, lg_h = _legend_size(ctx, ent, size_tick, "t")
        top += lg_h + pad
    if legend_pos == "b" and ent:
        _, lgh = _legend_size(ctx, ent, size_tick, "b")
        bottom -= lgh + pad

    left = x + pad
    right = x + w - pad
    if legend_pos == "r" and ent:
        lg_w, _ = _legend_size(ctx, ent, size_tick, "r")
        right -= lg_w + pad
    elif legend_pos == "l" and ent:
        lg_w, _ = _legend_size(ctx, ent, size_tick, "l")
        left += lg_w + pad

    # 顶点标签占位
    cat_labels = []
    for i in range(ncat):
        lab = ""
        for s in sers:
            if i < len(s["cat"]) and s["cat"][i] not in (None, ""):
                lab = str(s["cat"][i])
                break
        cat_labels.append(lab or str(i + 1))
    lab_w = max((_text_w(ctx, s, size_tick) for s in cat_labels), default=0.0)

    cx = (left + right) / 2.0
    cy = top + (bottom - top) / 2.0
    r = min((right - left) / 2.0 - lab_w * 0.5 - 2.0,
            (bottom - top) / 2.0 - size_tick * 1.2)
    r = max(3.0, r)

    def vpos(v):
        t = (v - v_lo) / (v_hi - v_lo)
        return r * min(1.0, max(0.0, t))

    def vert(i, rr):
        a = -math.pi / 2.0 + 2.0 * math.pi * i / max(1, ncat)
        return (cx + rr * math.cos(a), cy + rr * math.sin(a))

    # 同心多边形网格（4~5 圈）
    rings = ticks[1:] if len(ticks) > 1 else ticks
    for t in rings:
        rr = vpos(t)
        if rr <= 1e-9:
            continue
        ring = [vert(i, rr) for i in range(ncat)]
        _add_closed(ctx, ring)
    # 轴射线
    for i in range(ncat):
        _add(ctx, [(cx, cy), vert(i, r)])
    # 轴标
    for i in range(ncat):
        lx, ly = vert(i, r + size_tick * 1.15)
        _draw_text(ctx, cat_labels[i], lx, ly + size_tick * 0.36, size_tick,
                   align="center")
    # 刻度值（沿正上方射线右侧）
    if ax_val.get("tick", "nextto") != "none" and not ax_val.get("deleted"):
        for t in ticks:
            rr = vpos(t)
            _draw_text(ctx, _fmt_num(t), cx + 1.6, cy - rr + size_tick * 0.36,
                       size_tick * 0.85)

    # 系列闭合多边形
    for si, s in enumerate(sers):
        pts = []
        ok = True
        for i in range(ncat):
            v = s["val"][i] if i < len(s["val"]) else None
            if v is None or not math.isfinite(v):
                ok = False
                break
            pts.append(vert(i, vpos(v)))
        if not ok or len(pts) < 3:
            continue
        style = _STYLE_CYCLE[si % 4]
        closed = pts + [pts[0]]
        for seg in _line_style_split(closed, style):
            _add(ctx, seg)

    if ent and legend_pos in ("l", "r"):
        lg_w, lg_h = _legend_size(ctx, ent, size_tick, legend_pos)
        lx = x + w - pad - lg_w if legend_pos == "r" else x + pad
        ly = cy - lg_h / 2.0
        _draw_legend(ctx, ent, legend_pos, lx, ly, size_tick, lg_w + 1.0)
    elif ent and legend_pos == "b":
        _, lgh = _legend_size(ctx, ent, size_tick, "b")
        _draw_legend(ctx, ent, "b", x + pad, bottom - lgh, size_tick, w - 2 * pad)


# ---------------------------------------------------------------- 入口

_GROUP_KINDS = {
    "lineChart": "line",
    "scatterChart": "scatter",
    "areaChart": "area",
    "barChart": "bar",
    "pieChart": "pie",
    "doughnutChart": "doughnut",
    "radarChart": "radar",
    "bubbleChart": "bubble",
}


def _fallback_axis(plot_area, prefer: str):
    for tag in ((prefer, "valAx", "catAx") if prefer == "valAx" else (prefer, "catAx", "valAx")):
        for c in plot_area:
            if _local(c.tag) == tag:
                return _axis_info(c)
    return {}


def draw_chart(ctx, data: bytes, x: float, y: float, w: float, h: float) -> int:
    """c:chartSpace XML → 手绘图表笔画画进 ctx.strokes。x,y=图框左上(mm)，w,h=框尺寸；
    内容等比适配留边（标题/图例/轴标分配空间）。返回笔画数。
    支持：line / scatter / area / bar(含 stacked、percentStacked) / pie / doughnut / radar。
    线型编码（多系列轮转）：实线 / 虚线(3,1.5mm) / 点线(0.6,1.2) / 点划(3,1.2,0.5,1.2)——
    与 img2stroke 图片版同规则；虚线烘焙为独立短线段（gap 处自然抬笔）。
    文本（标题/轴刻度/图例/数据标签）用写字字库书写（draw_char），字号按框缩放（下限 1.8mm）。
    解析/绘制异常时返回 0 不抛。"""
    n0 = len(ctx.strokes)
    try:
        _draw_chart_impl(ctx, data, x, y, w, h)
    except Exception:
        del ctx.strokes[n0:]
        return 0
    return len(ctx.strokes) - n0


def _draw_chart_impl(ctx, data, x, y, w, h) -> None:
    if not data or w <= 0 or h <= 0:
        return
    root = ET.fromstring(data)
    if _local(root.tag) not in ("chartSpace", "chart"):
        # 允许外层包装：找首个 chartSpace/chart
        root = _desc(root, "chartSpace") or _desc(root, "chart") or root
    chart_el = _kid(root, "chart") or root
    plot_area = _desc(chart_el, "plotArea")
    if plot_area is None:
        return

    group = None
    kind = None
    for c in plot_area:
        k = _GROUP_KINDS.get(_local(c.tag))
        if k:
            group, kind = c, k
            break
    if group is None:
        return

    sers = _extract_series(group)
    if not sers:
        return

    # 过滤无有效数据的系列
    def usable(s):
        if kind in ("scatter", "bubble"):
            return any(xv is not None and yv is not None
                       for xv, yv in zip(s["x"], s["val"]))
        return any(v is not None and math.isfinite(v) for v in s["val"])
    sers = [s for s in sers if usable(s)]
    if not sers:
        return

    if kind == "pie":
        vals = sers[0]["val"]
        if not any(v is not None and math.isfinite(v) and v > 0 for v in vals):
            return
    if kind == "doughnut":
        vals = sers[0]["val"]
        if not any(v is not None and math.isfinite(v) and v > 0 for v in vals):
            return

    title = _chart_title_text(chart_el)
    legend_pos = _legend_pos(chart_el)

    ncat = max((len(s["val"]) for s in sers), default=0)
    for s in sers:
        ncat = max(ncat, len(s["cat"]))

    if kind in ("pie", "doughnut"):
        _render_pie(ctx, kind, group, sers, x, y, w, h, title, legend_pos)
        return
    if kind == "radar":
        ax_ids = _group_ax_ids(group)
        ax_val = {}
        if len(ax_ids) >= 2:
            el = _find_axis(plot_area, ax_ids[1])
            if el is not None:
                ax_val = _axis_info(el)
        if not ax_val:
            ax_val = _fallback_axis(plot_area, "valAx")
        _render_radar(ctx, sers, ncat, x, y, w, h, title, legend_pos, ax_val)
        return

    ax_ids = _group_ax_ids(group)
    ax_cat_el = _find_axis(plot_area, ax_ids[0]) if ax_ids else None
    ax_val_el = _find_axis(plot_area, ax_ids[1]) if len(ax_ids) > 1 else None
    if kind in ("scatter", "bubble"):
        # 散点：axIds[0]=数值 x 轴，axIds[1]=数值 y 轴
        ax_x = _axis_info(ax_cat_el) if ax_cat_el is not None else {}
        ax_y = _axis_info(ax_val_el) if ax_val_el is not None else {}
        if not ax_x:
            ax_x = _fallback_axis(plot_area, "valAx")
        if not ax_y:
            ax_y = _fallback_axis(plot_area, "valAx")
        # axPos 校正：b/t → x，l/r → y
        for a in (ax_x, ax_y):
            if a.get("pos") in ("l", "r"):
                ax_y = a
            elif a.get("pos") in ("b", "t"):
                ax_x = a
        _render_num_chart(ctx, kind, sers, x, y, w, h, title, legend_pos, ax_x, ax_y)
        return

    ax_cat = _axis_info(ax_cat_el) if ax_cat_el is not None else _fallback_axis(plot_area, "catAx")
    ax_val = _axis_info(ax_val_el) if ax_val_el is not None else _fallback_axis(plot_area, "valAx")
    _render_cat_chart(ctx, kind, group, sers, ncat, x, y, w, h,
                      title, legend_pos, ax_cat, ax_val)
