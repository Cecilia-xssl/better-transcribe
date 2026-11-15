"""drawml.py —— Word 形状（w:drawing / w:pict）→ 轮廓折线（mm，形状框左上=(0,0)）。

三类几何来源：
- a:prstGeom：参数形轮廓表（[0,1]² 归一坐标生成后按 w/h 缩放）；
- a:custGeom/a:pathLst：路径段拼 SVG d 串，经 img2stroke.svg_to_strokes 折线化；
- VML v:shape（v:path/@d 或 @path 属性）：d 串归一化后同样走 svg_to_strokes。
填充不画只轮廓；a:headEnd/a:tailEnd 端点加 V 形箭头短线；w:txbxContent 段落原样返回。
"""

from __future__ import annotations

import math
import re

EMU2MM = 1.0 / 36000.0
PT2MM = 25.4 / 72.0

_NUM_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")

# 端点 V 形箭头：短臂长 mm、张角°
_ARROW_LEN = 2.5
_ARROW_ANG = 25.0


# ---------------------------------------------------------------- 基础工具

def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    if ":" in tag:
        return tag.rsplit(":", 1)[1]
    return tag


def _kids(el, name: str):
    return [c for c in el if _local(c.tag) == name]


def _kid(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _attr(el, name: str, default: str | None = None) -> str | None:
    """取属性（含命名空间形式，按局部名匹配）。"""
    if el is None:
        return default
    v = el.get(name)
    if v is not None:
        return v
    for k, val in el.attrib.items():
        if _local(k) == name:
            return val
    return default


def _f(el, name: str, default: float = 0.0) -> float:
    s = _attr(el, name)
    if s is None:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _text_f(el, default: float = 0.0) -> float:
    if el is None or el.text is None:
        return default
    try:
        return float(el.text.strip())
    except ValueError:
        return default


def _pt(el):
    """a:pt 元素 → (x, y)。"""
    if el is None:
        return (0.0, 0.0)
    return (_f(el, "x"), _f(el, "y"))


# ---------------------------------------------------------------- 折线后处理

def _poly_closed(pts):
    if pts and (abs(pts[0][0] - pts[-1][0]) > 1e-9 or abs(pts[0][1] - pts[-1][1]) > 1e-9):
        return pts + [pts[0]]
    return pts


def _rotate_pts(polylines, w: float, h: float, deg: float):
    if not deg or abs(deg) < 1e-9:
        return polylines
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    cx, cy = w / 2.0, h / 2.0
    out = []
    for pts in polylines:
        npts = []
        for x, y in pts:
            dx, dy = x - cx, y - cy
            npts.append((cx + dx * c - dy * s, cy + dx * s + dy * c))
        out.append(npts)
    return out


def _barb(tip, toward, length: float, ang_deg: float):
    """端点 V 形箭头短臂折线（tip 处张开 ±ang_deg）。"""
    dx = tip[0] - toward[0]
    dy = tip[1] - toward[1]
    n = math.hypot(dx, dy)
    if n <= 1e-9:
        return [tip, (tip[0] + length, tip[1])]
    ux, uy = dx / n, dy / n
    a = math.radians(ang_deg)
    c, s = math.cos(a), math.sin(a)
    b1 = (tip[0] - length * (ux * c - uy * s), tip[1] - length * (ux * s + uy * c))
    b2 = (tip[0] - length * (ux * c + uy * s), tip[1] - length * (-ux * s + uy * c))
    return [b1, tip, b2]


def _add_arrowheads(polylines, head: bool, tail: bool):
    if not (head or tail):
        return polylines
    out = []
    for pts in polylines:
        out.append(pts)
        if len(pts) < 2:
            continue
        if head:
            out.append(_barb(pts[0], pts[1], _ARROW_LEN, _ARROW_ANG))
        if tail:
            out.append(_barb(pts[-1], pts[-2], _ARROW_LEN, _ARROW_ANG))
    return out


# ---------------------------------------------------------------- prstGeom 参数形

def _prst_polylines(prst: str, w: float, h: float):
    """[0,1]² 归一参数轮廓 → 按 w/h 缩放的局部 mm 折线。未收录返回 None。"""
    pts = _prst_unit(prst)
    if pts is None:
        return None
    out = []
    for poly in pts:
        scaled = [(x * w, y * h) for x, y in poly]
        if len(scaled) >= 2:
            out.append(scaled)
    return out


def _prst_unit(prst: str):
    """参数方程生成 [0,1]² 轮廓（y 向下）。闭合形末点=首点。"""
    p = (prst or "").strip()
    if p == "rect":
        return [_poly_closed([(0, 0), (1, 0), (1, 1), (0, 1)])]
    if p == "roundRect":
        return [_roundrect_unit(0.18, 5)]
    if p == "ellipse":
        n = 36
        pts = [(0.5 + 0.5 * math.cos(2 * math.pi * i / n), 0.5 + 0.5 * math.sin(2 * math.pi * i / n))
               for i in range(n)]
        return [_poly_closed(pts)]
    if p == "line":
        return [[(0, 0), (1, 1)]]
    if p == "triangle":
        return [_poly_closed([(0, 1), (0.5, 0), (1, 1)])]
    if p == "rightArrow":
        return [_poly_closed([(0, 0.35), (0.6, 0.35), (0.6, 0), (1, 0.5),
                              (0.6, 1), (0.6, 0.65), (0, 0.65)])]
    if p == "leftArrow":
        return [_poly_closed([(1, 0.35), (0.4, 0.35), (0.4, 0), (0, 0.5),
                              (0.4, 1), (0.4, 0.65), (1, 0.65)])]
    if p == "diamond":
        return [_poly_closed([(0.5, 0), (1, 0.5), (0.5, 1), (0, 0.5)])]
    if p in ("pentagon", "regularPentagon"):
        return [_poly_closed(_regular_ngon(5, -math.pi / 2))]
    if p in ("hexagon", "regularHexagon"):
        return [_poly_closed(_regular_ngon(6, 0.0))]
    if p == "star5":
        return [_poly_closed(_star_unit(5, 0.5, 0.5 * 0.382))]
    if p == "chevron":
        return [_poly_closed([(0, 0), (0.75, 0), (1, 0.5), (0.75, 1), (0, 1), (0.25, 0.5)])]
    if p == "trapezoid":
        return [_poly_closed([(0.2, 0), (0.8, 0), (1, 1), (0, 1)])]
    if p == "parallelogram":
        return [_poly_closed([(0.25, 0), (1, 0), (0.75, 1), (0, 1)])]
    if p == "octagon":
        k = 0.2929  # 1 - 1/sqrt(2)
        return [_poly_closed([(k, 0), (1 - k, 0), (1, k), (1, 1 - k),
                              (1 - k, 1), (k, 1), (0, 1 - k), (0, k)])]
    return None


def _regular_ngon(n: int, a0: float):
    return [(0.5 + 0.5 * math.cos(a0 + 2 * math.pi * i / n),
             0.5 + 0.5 * math.sin(a0 + 2 * math.pi * i / n)) for i in range(n)]


def _star_unit(n: int, r_out: float, r_in: float):
    pts = []
    for k in range(2 * n):
        ang = -math.pi / 2 + k * math.pi / n
        r = r_out if k % 2 == 0 else r_in
        pts.append((0.5 + r * math.cos(ang), 0.5 + r * math.sin(ang)))
    return pts


def _roundrect_unit(r: float, seg: int):
    pts = []

    def arc(cx, cy, a0, a1):
        for i in range(seg + 1):
            t = a0 + (a1 - a0) * i / seg
            pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))

    pts.append((r, 0.0))
    pts.append((1 - r, 0.0))
    arc(1 - r, r, -math.pi / 2, 0.0)          # 右上
    pts.append((1.0, 1 - r))
    arc(1 - r, 1 - r, 0.0, math.pi / 2)       # 右下
    pts.append((r, 1.0))
    arc(r, 1 - r, math.pi / 2, math.pi)       # 左下
    pts.append((0.0, r))
    arc(r, r, math.pi, 3 * math.pi / 2)       # 左上
    return _poly_closed(pts)


# ---------------------------------------------------------------- SVG d 串 → 折线

def _d_to_polylines(d: str, pw: float, ph: float, w: float, h: float):
    """路径 d 串（pw×ph 路径坐标）→ 形状框 w×h mm 折线（经 img2stroke 折线化）。"""
    if not d or not d.strip() or w <= 0 or h <= 0:
        return []
    sx = (w / pw) if pw > 1e-9 else 1.0
    sy = (h / ph) if ph > 1e-9 else 1.0
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.6f} {h:.6f}">'
        f'<path d="{d}" transform="scale({sx:.9f},{sy:.9f})" '
        f'fill="none" stroke="black"/></svg>'
    )
    from img2stroke import svg_to_strokes
    return svg_to_strokes(svg.encode("utf-8"), w, h)


def _normalize_d(d: str) -> str:
    """VML d 串 → SVG d 串：x/X→Z、e/E 截断（end），其余命令透传。"""
    toks = []
    i, n = 0, len(d)
    while i < n:
        ch = d[i]
        if ch.isalpha():
            cl = ch.lower()
            if cl == "e":
                break
            toks.append("Z" if cl == "x" else ch)
            i += 1
            continue
        m = _NUM_RE.match(d, i)
        if m:
            toks.append(m.group(0))
            i = m.end()
        else:
            i += 1
    return " ".join(toks)


# ---------------------------------------------------------------- custGeom

def _custgeom_d(path_els) -> tuple[str, float, float]:
    """a:path 列表 → (SVG d 串, 路径宽, 路径高)。"""
    ds = []
    vb_w = vb_h = 0.0
    for path in path_els:
        pw = _f(path, "w")
        ph = _f(path, "h")
        vb_w = max(vb_w, pw)
        vb_h = max(vb_h, ph)
        cur = (0.0, 0.0)
        start = (0.0, 0.0)
        for cmd in path:
            tag = _local(cmd.tag)
            if tag == "moveTo":
                pt = _pt(_kid(cmd, "pt"))
                ds.append("M%.4f,%.4f" % pt)
                cur = start = pt
            elif tag == "lnTo":
                pt = _pt(_kid(cmd, "pt"))
                ds.append("L%.4f,%.4f" % pt)
                cur = pt
            elif tag == "cubicBezTo":
                pts = [_pt(p) for p in _kids(cmd, "pt")]
                if len(pts) == 3:
                    ds.append("C%.4f,%.4f %.4f,%.4f %.4f,%.4f"
                              % (pts[0][0], pts[0][1], pts[1][0], pts[1][1], pts[2][0], pts[2][1]))
                    cur = pts[2]
            elif tag == "quadBezTo":
                pts = [_pt(p) for p in _kids(cmd, "pt")]
                if len(pts) == 2:
                    ds.append("Q%.4f,%.4f %.4f,%.4f"
                              % (pts[0][0], pts[0][1], pts[1][0], pts[1][1]))
                    cur = pts[1]
            elif tag == "arcTo":
                # DrawingML arcTo（wR hR stAng swAng，角=1/60000°）→ 采样折线近似
                wr = _f(cmd, "wR")
                hr = _f(cmd, "hR")
                st = math.radians(_f(cmd, "stAng") / 60000.0)
                sw = math.radians(_f(cmd, "swAng") / 60000.0)
                if wr > 0 and hr > 0 and abs(sw) > 1e-12:
                    cx = cur[0] - wr * math.cos(st)
                    cy = cur[1] - hr * math.sin(st)
                    nseg = max(4, int(abs(sw) / (math.pi / 8.0)) + 1)
                    for i in range(1, nseg + 1):
                        t = st + sw * i / nseg
                        ds.append("L%.4f,%.4f" % (cx + wr * math.cos(t), cy + hr * math.sin(t)))
                    cur = (cx + wr * math.cos(st + sw), cy + hr * math.sin(st + sw))
            elif tag == "close":
                ds.append("Z")
                cur = start
    return " ".join(ds), vb_w, vb_h


# ---------------------------------------------------------------- VML

def _parse_style_dims(style: str) -> dict[str, float]:
    """VML style 属性 → {key: mm}（支持 pt/px/无后缀=pt）。"""
    out = {}
    for part in (style or "").split(";"):
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        k = k.strip().lower()
        if k not in ("left", "top", "width", "height"):
            continue
        v = v.strip().lower().replace("pt", "").replace("px", "").strip()
        try:
            out[k] = float(v) * PT2MM
        except ValueError:
            continue
    return out


def _parse_vml(v_el):
    """v:shape / v:rect / v:oval → parse_shape 结果 dict。"""
    dims = _parse_style_dims(_attr(v_el, "style", "") or "")
    w = dims.get("width", 0.0)
    h = dims.get("height", 0.0)
    if w <= 0 or h <= 0:
        # 退化：a:ext / wp:extent
        for c in v_el.iter():
            lt = _local(c.tag)
            if lt == "extent" and _attr(c, "cx"):
                w = _f(c, "cx") * EMU2MM
                h = _f(c, "cy") * EMU2MM
                break
            if lt == "ext" and _attr(c, "cx"):
                w = _f(c, "cx") * EMU2MM
                h = _f(c, "cy") * EMU2MM
                break
    x = dims.get("left") if "left" in dims else None
    y = dims.get("top") if "top" in dims else None

    polylines = None
    d_raw = None
    vpath = _kid(v_el, "path")
    if vpath is not None and _attr(vpath, "d"):
        d_raw = _attr(vpath, "d")
    if not d_raw:
        d_raw = _attr(v_el, "path")
    if d_raw:
        cw, ch = 1.0, 1.0
        cs = _attr(v_el, "coordsize") or ""
        if cs and "," in cs:
            try:
                a, b = cs.split(",", 1)
                cw, ch = float(a), float(b)
            except ValueError:
                cw = ch = 1.0
        polylines = _d_to_polylines(_normalize_d(d_raw), cw, ch, w, h)
    else:
        lt = _local(v_el.tag)
        if lt == "rect":
            polylines = _prst_polylines("rect", w, h)
        elif lt == "oval":
            polylines = _prst_polylines("ellipse", w, h)

    return {
        "polylines": polylines,
        "x": x, "y": y,
        "w": w, "h": h,
        "txbx": _collect_txbx(v_el),
    }


def _collect_txbx(el):
    """w:txbxContent 内的 w:p 段落元素列表；无则 None。"""
    for c in el.iter():
        if _local(c.tag) == "txbxContent":
            paras = _kids(c, "p")
            return paras if paras else []
    return None


# ---------------------------------------------------------------- DrawingML 形状

def _parse_drawing(node):
    """wp:inline / wp:anchor → parse_shape 结果 dict。"""
    is_anchor = _local(node.tag) == "anchor"

    w = h = 0.0
    ext = _kid(node, "extent")
    if ext is not None:
        w = _f(ext, "cx") * EMU2MM
        h = _f(ext, "cy") * EMU2MM

    x = y = None
    if is_anchor:
        for name, slot in (("positionH", "x"), ("positionV", "y")):
            pos = _kid(node, name)
            if pos is None:
                continue
            off = _kid(pos, "posOffset")
            val = _text_f(off, None) if off is not None else None
            if val is not None:
                if slot == "x":
                    x = val * EMU2MM
                else:
                    y = val * EMU2MM

    # 找形状本体：优先 wps:wsp，退化到 VML v:shape
    wsp = None
    vshape = None
    for c in node.iter():
        lt = _local(c.tag)
        if lt == "wsp" and wsp is None:
            wsp = c
        elif lt in ("shape", "rect", "oval") and vshape is None:
            vshape = c

    if wsp is None and vshape is not None:
        res = _parse_vml(vshape)
        if w > 0 and h > 0:
            res["w"], res["h"] = w, h
        if is_anchor:
            res["x"], res["y"] = x, y
        return res
    if wsp is None:
        return None

    # 尺寸回退：a:xfrm/a:ext（与 extent 冲突时以 extent 为准）
    xfrm = None
    for c in wsp.iter():
        if _local(c.tag) == "xfrm":
            xfrm = c
            break
    if w <= 0 or h <= 0:
        if xfrm is not None:
            ext2 = _kid(xfrm, "ext")
            if ext2 is not None:
                w = _f(ext2, "cx") * EMU2MM
                h = _f(ext2, "cy") * EMU2MM

    # 几何：prstGeom / custGeom
    prst_el = None
    cust_el = None
    ln_el = None
    for c in wsp.iter():
        lt = _local(c.tag)
        if lt == "prstGeom" and prst_el is None:
            prst_el = c
        elif lt == "custGeom" and cust_el is None:
            cust_el = c
        elif lt == "ln" and ln_el is None:
            ln_el = c

    polylines = None
    if prst_el is not None:
        prst = _attr(prst_el, "prst", "") or ""
        polylines = _prst_polylines(prst, w, h)
    elif cust_el is not None:
        path_els = [p for p in cust_el.iter() if _local(p.tag) == "path"]
        d, pw, ph = _custgeom_d(path_els)
        polylines = _d_to_polylines(d, pw, ph, w, h)

    # 端点箭头
    if polylines and ln_el is not None:
        head = _kid(ln_el, "headEnd") is not None
        tail = _kid(ln_el, "tailEnd") is not None
        if head or tail:
            ht = _kid(ln_el, "headEnd")
            tt = _kid(ln_el, "tailEnd")
            head = head and (ht is not None and (_attr(ht, "type", "") or "triangle") != "none")
            tail = tail and (tt is not None and (_attr(tt, "type", "") or "triangle") != "none")
            polylines = _add_arrowheads(polylines, head, tail)

    # 可选旋转（a:xfrm/@rot，1/60000°）
    if polylines and xfrm is not None:
        rot = _f(xfrm, "rot", 0.0) / 60000.0
        polylines = _rotate_pts(polylines, w, h, rot)

    return {
        "polylines": polylines,
        "x": x, "y": y,
        "w": w, "h": h,
        "txbx": _collect_txbx(wsp),
    }


# ---------------------------------------------------------------- 公共 API

def parse_shape(el) -> dict | None:
    """一个 w:drawing 子树（wp:inline 或 wp:anchor 内含 wps:wsp）或 w:pict/v:shape →
    {"polylines": [[(x,y)...], ...],   # 局部坐标 mm，形状框左上=(0,0)
     "x": float|None, "y": float|None,  # 锚定浮动=相对页面 mm 绝对坐标；inline=None（随流）
     "w": float, "h": float,            # 形状尺寸 mm
     "txbx": [el, ...] | None}          # w:txbxContent 内的 w:p 段落元素列表

    未收录的 prst → polylines=None 但保留 txbx/尺寸（调用方只排文字）。
    非形状节点（图片等）→ None。
    """
    if el is None:
        return None
    tag = _local(el.tag)

    if tag == "drawing":
        node = _kid(el, "inline")
        if node is None:
            node = _kid(el, "anchor")
        if node is None:
            for c in el.iter():
                if _local(c.tag) in ("inline", "anchor"):
                    node = c
                    break
        if node is None:
            return None
        return _parse_drawing(node)

    if tag in ("inline", "anchor"):
        return _parse_drawing(el)

    if tag == "pict":
        for c in el.iter():
            if _local(c.tag) in ("shape", "rect", "oval", "line"):
                return _parse_vml(c)
        return None

    if tag in ("shape", "rect", "oval", "line"):
        return _parse_vml(el)

    # 其他根：向下找 w:drawing / w:pict
    for c in el.iter():
        lt = _local(c.tag)
        if lt == "drawing":
            return parse_shape(c)
        if lt == "pict":
            return parse_shape(c)
    return None
