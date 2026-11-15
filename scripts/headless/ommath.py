"""ommath.py —— OMML 公式 → 二维排版折线（print-like，非线性化）。

递归盒子算法：节点返回 (w, ascent, descent)，绘制自左向右，内部元素顺序 = XML 子元素顺序。
支持：m:r/m:t 文本、m:f 分数、m:sSup/m:sSub/m:sSubSup 上下标、m:rad 根号、
m:d 定界符（括号按内容高拉伸折线）、m:nary 求和/积分（手绘折线或大字形）+ 上下限。
度量 layout.char_advance_mm、绘制 layout.draw_char（模块内惰性导入，避免循环依赖）。
空/未知子元素跳过并继续（不抛）。
"""

from __future__ import annotations

import math

# 文本盒（em 倍数，覆盖 gfont 字身 y∈[-450,+21]/470 并留余量）
TEXT_ASC = 1.1
TEXT_DESC = 0.15
# 上下标
SUBSUP_RATIO = 0.7
SUBSUP_SHIFT = 0.35
# 分数
FRAC_RATIO = 0.8
FRAC_GAP = 0.10
FRAC_BAR = 0.35       # 中横线在基线上方（em）
FRAC_PAD = 0.15
# 根号
RAD_HW = 0.55         # 勾线宽（em）
RAD_TOP = 0.20        # 顶线高出被开方数（em）
RAD_OV = 0.20         # 顶线右出头（em）
RAD_DEG_RATIO = 0.55
# 定界符
DELIM_MIN_H = 0.55    # 括号最小高（em）
# nary
NARY_SYM_W = 0.85
NARY_TOP = 0.75       # 符号顶部高出基线（×H）
NARY_BOT = 0.25       # 符号底部低于基线（×H）
NARY_MIN_H = 1.5
NARY_LIM_RATIO = 0.7
NARY_GAP = 0.10


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
    if el is None:
        return default
    v = el.get(name)
    if v is not None:
        return v
    for k, val in el.attrib.items():
        if _local(k) == name:
            return val
    return default


def _val_of(el, default: str = "") -> str:
    """m:xxxPr 子元素（如 m:begChr/m:chr）的 m:val 属性。"""
    return _attr(el, "val", default) or default


def _adv(ctx, ch: str, size: float) -> float:
    """字符步进：有 ctx 走 layout.char_advance_mm，否则按 em 估计。"""
    if ctx is not None:
        from layout import char_advance_mm
        return char_advance_mm(ctx, ch, size)
    if ch == " ":
        return size * 0.35
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9"):
        return size * 0.55
    return size * 0.9


def _draw_line(ctx, pts) -> None:
    from layout import Stroke
    if pts and len(pts) >= 2:
        ctx.strokes.append(Stroke(list(pts)))


# ---------------------------------------------------------------- 文本

def _run_text(el) -> str:
    """m:r / m:t / 任意含 m:t 的节点 → 展开后的文本。"""
    tag = _local(el.tag)
    if tag == "t":
        from layout import expand_chars
        return expand_chars(el.text or "")
    parts = []
    for c in el:
        if _local(c.tag) == "t" and c.text:
            parts.append(c.text)
    if parts:
        from layout import expand_chars
        return expand_chars("".join(parts))
    return ""


def _text_box(text: str, size: float, x: float, baseline: float, ctx, draw: bool):
    w = 0.0
    for ch in text:
        if draw and ctx is not None:
            from layout import draw_char
            w += draw_char(ctx, ch, x + w, baseline, size)
        else:
            w += _adv(ctx, ch, size)
    return w, TEXT_ASC * size, TEXT_DESC * size


# ---------------------------------------------------------------- 括号折线

def _bracket_pts(ch: str, w: float, y_top: float, y_bot: float):
    H = y_bot - y_top
    if ch == ")":
        return [(0, y_top), (w * 0.65, y_top + 0.22 * H), (w, y_top + 0.5 * H),
                (w * 0.65, y_top + 0.78 * H), (0, y_bot)]
    if ch == "[":
        return [(w, y_top), (w * 0.25, y_top), (w * 0.25, y_bot), (w, y_bot)]
    if ch == "]":
        return [(0, y_top), (w * 0.75, y_top), (w * 0.75, y_bot), (0, y_bot)]
    if ch == "|":
        return [(w * 0.5, y_top), (w * 0.5, y_bot)]
    if ch == "{":
        return [(w, y_top), (w * 0.45, y_top), (w * 0.22, y_top + 0.22 * H),
                (w * 0.42, y_top + 0.5 * H), (w * 0.16, y_top + 0.5 * H),
                (w * 0.42, y_top + 0.5 * H), (w * 0.22, y_top + 0.78 * H),
                (w * 0.45, y_bot), (w, y_bot)]
    if ch == "}":
        return [(0, y_top), (w * 0.55, y_top), (w * 0.78, y_top + 0.22 * H),
                (w * 0.58, y_top + 0.5 * H), (w * 0.84, y_top + 0.5 * H),
                (w * 0.58, y_top + 0.5 * H), (w * 0.78, y_top + 0.78 * H),
                (w * 0.55, y_bot), (0, y_bot)]
    # 默认 / "("
    return [(w, y_top), (w * 0.35, y_top + 0.22 * H), (0, y_top + 0.5 * H),
            (w * 0.35, y_top + 0.78 * H), (w, y_bot)]


# ---------------------------------------------------------------- nary 符号

def _nary_symbol(chr_val: str, w: float, top: float, bot: float):
    """手绘大符号折线；不认识的返回 None（调用方退回大字形）。"""
    h = bot - top
    if chr_val in ("\u2211", "\u03a3", "\u03c3"):
        return [[(0.92 * w, top + 0.04 * h), (0.10 * w, top + 0.04 * h),
                 (0.55 * w, top + 0.50 * h), (0.10 * w, top + 0.96 * h),
                 (0.92 * w, top + 0.96 * h)]]
    if chr_val in ("\u222b", "\u222c", "\u222d"):
        return [[(0.62 * w, top + 0.02 * h), (0.42 * w, top + 0.10 * h),
                 (0.32 * w, top + 0.32 * h), (0.50 * w, top + 0.50 * h),
                 (0.68 * w, top + 0.68 * h), (0.58 * w, top + 0.90 * h),
                 (0.38 * w, top + 0.98 * h)]]
    if chr_val in ("\u220f", "\u03a0", "\u2210", "\u22c0", "\u22c1", "\u22c2", "\u22c3"):
        return [[(0.12 * w, top + 0.98 * h), (0.12 * w, top + 0.04 * h),
                 (0.88 * w, top + 0.04 * h), (0.88 * w, top + 0.98 * h)]]
    return None


# ---------------------------------------------------------------- 递归盒子

_CONTAINERS = {"oMath", "oMathPara", "e", "num", "den", "sub", "sup", "deg",
               "base", "fName", "numBox"}


def _walk(el, size: float, x: float, baseline: float, ctx, draw: bool):
    """节点 → (w, ascent, descent)；draw=True 时同步落笔。未知/空 → (0,0,0)。"""
    if el is None or size <= 0:
        return 0.0, 0.0, 0.0
    tag = _local(el.tag)

    if tag in _CONTAINERS:
        return _layout_seq(el, size, x, baseline, ctx, draw)

    if tag in ("r", "t"):
        text = _run_text(el)
        if not text:
            return 0.0, 0.0, 0.0
        return _text_box(text, size, x, baseline, ctx, draw)

    if tag == "f":
        return _layout_frac(el, size, x, baseline, ctx, draw)
    if tag == "sSup":
        return _layout_script(el, size, x, baseline, ctx, draw, sup=True, sub=False)
    if tag == "sSub":
        return _layout_script(el, size, x, baseline, ctx, draw, sup=False, sub=True)
    if tag == "sSubSup":
        return _layout_script(el, size, x, baseline, ctx, draw, sup=True, sub=True)
    if tag == "rad":
        return _layout_rad(el, size, x, baseline, ctx, draw)
    if tag == "d":
        return _layout_delim(el, size, x, baseline, ctx, draw)
    if tag == "nary":
        return _layout_nary(el, size, x, baseline, ctx, draw)

    # 未知元素：跳过（含可展开文本时兜底当文本）
    text = _run_text(el)
    if text:
        return _text_box(text, size, x, baseline, ctx, draw)
    return 0.0, 0.0, 0.0


def _layout_seq(parent, size, x, baseline, ctx, draw):
    """子元素按 XML 顺序自左向右拼接。"""
    w = 0.0
    asc = 0.0
    desc = 0.0
    cx = x
    for ch in parent:
        cw, ca, cd = _walk(ch, size, cx, baseline, ctx, draw)
        if cw <= 0.0 and ca <= 0.0 and cd <= 0.0:
            continue
        cx += cw
        w += cw
        asc = max(asc, ca)
        desc = max(desc, cd)
    return w, asc, desc


def _layout_frac(el, size, x, baseline, ctx, draw):
    """分数：分子/分母 0.8× 子号上下叠 + 中横线。"""
    cs = size * FRAC_RATIO
    num_el = _kid(el, "num")
    den_el = _kid(el, "den")
    nw, na, nd = _walk(num_el, cs, 0.0, 0.0, ctx, False)
    dw, da, dd = _walk(den_el, cs, 0.0, 0.0, ctx, False)

    gap = FRAC_GAP * size
    pad = FRAC_PAD * size
    w = max(nw, dw) + 2.0 * pad
    bar_y = baseline - FRAC_BAR * size
    num_base = bar_y - gap - nd
    den_base = bar_y + gap + da
    asc = FRAC_BAR * size + gap + na + nd
    desc = max(0.0, -FRAC_BAR * size + gap + da + dd)

    if draw:
        _draw_line(ctx, [(x + pad * 0.5, bar_y), (x + w - pad * 0.5, bar_y)])
        if num_el is not None:
            _walk(num_el, cs, x + (w - nw) / 2.0, num_base, ctx, True)
        if den_el is not None:
            _walk(den_el, cs, x + (w - dw) / 2.0, den_base, ctx, True)
    return w, asc, desc


def _layout_script(el, size, x, baseline, ctx, draw, sup: bool, sub: bool):
    """上下标：0.7× 子号，基线 ±0.35em。"""
    cs = size * SUBSUP_RATIO
    shift = SUBSUP_SHIFT * size
    base_el = _kid(el, "e")
    sup_el = _kid(el, "sup") if sup else None
    sub_el = _kid(el, "sub") if sub else None

    bw, ba, bd = _walk(base_el, size, 0.0, 0.0, ctx, False)
    pw, pa, pd = (0.0, 0.0, 0.0)
    sw, sa, sd = (0.0, 0.0, 0.0)
    if sup_el is not None:
        pw, pa, pd = _walk(sup_el, cs, 0.0, 0.0, ctx, False)
    if sub_el is not None:
        sw, sa, sd = _walk(sub_el, cs, 0.0, 0.0, ctx, False)

    w = bw + max(pw, sw)
    asc = max(ba, (shift + pa) if sup else 0.0, max(0.0, sa - shift) if sub else 0.0)
    desc = max(bd, (shift + sd) if sub else 0.0, max(0.0, pd - shift) if sup else 0.0)

    if draw:
        if base_el is not None:
            _walk(base_el, size, x, baseline, ctx, True)
        if sup_el is not None:
            _walk(sup_el, cs, x + bw, baseline - shift, ctx, True)
        if sub_el is not None:
            _walk(sub_el, cs, x + bw, baseline + shift, ctx, True)
    return w, asc, desc


def _layout_rad(el, size, x, baseline, ctx, draw):
    """根号：勾线 + 被开方数顶线。deg 有内容时画在左上缺口。"""
    deg_el = _kid(el, "deg")
    e_el = _kid(el, "e")
    has_deg = False
    if deg_el is not None:
        for c in deg_el:
            if _local(c.tag) in ("r", "t", "f", "sSup", "sSub", "sSubSup", "rad",
                                 "d", "nary", "e", "num", "den"):
                has_deg = True
                break

    ds = size * RAD_DEG_RATIO
    dw, da, dd = (0.0, 0.0, 0.0)
    if has_deg:
        dw, da, dd = _walk(deg_el, ds, 0.0, 0.0, ctx, False)
    rw, ra, rd = _walk(e_el, size, 0.0, 0.0, ctx, False)

    hw = RAD_HW * size
    ov = RAD_OV * size
    top_off = RAD_TOP * size
    w = dw + hw + rw + ov
    asc = ra + top_off + max(0.0, da - dd)
    desc = max(rd, 0.0)

    if draw:
        xh = x + dw
        y_top = baseline - ra - top_off
        hook = [
            (xh, baseline - ra * 0.45),
            (xh + hw * 0.38, baseline + rd * 0.15),
            (xh + hw * 0.72, y_top),
            (xh + hw + rw + ov, y_top),
        ]
        _draw_line(ctx, hook)
        if e_el is not None:
            _walk(e_el, size, xh + hw, baseline, ctx, True)
        if has_deg:
            _walk(deg_el, ds, x, baseline - ra - top_off + dd, ctx, True)
    return w, asc, desc


def _layout_delim(el, size, x, baseline, ctx, draw):
    """定界符：括号按内容高拉伸折线。"""
    dpr = _kid(el, "dPr")
    beg = "("
    end = ")"
    if dpr is not None:
        beg = _val_of(_kid(dpr, "begChr"), "(") or "("
        end = _val_of(_kid(dpr, "endChr"), ")") or ")"

    e_els = _kids(el, "e")
    cw = 0.0
    ca = 0.0
    cd = 0.0
    for e in e_els:
        w2, a2, d2 = _walk(e, size, 0.0, 0.0, ctx, False)
        cw += w2
        ca = max(ca, a2)
        cd = max(cd, d2)

    H = max(ca + cd, DELIM_MIN_H * size)
    y_mid = (cd - ca) / 2.0
    y_top = y_mid - H / 2.0
    y_bot = y_mid + H / 2.0
    bw = 0.28 * size + 0.05 * H
    w = bw + cw + bw
    asc = max(ca, -y_top)
    desc = max(cd, y_bot)

    if draw:
        _draw_line(ctx, [(x + px, y) for px, y in _bracket_pts(beg, bw, baseline + y_top, baseline + y_bot)])
        cx = x + bw
        for e in e_els:
            w2, _, _ = _walk(e, size, cx, baseline, ctx, True)
            cx += w2
        _draw_line(ctx, [(x + bw + cw + px, y) for px, y in _bracket_pts(end, bw, baseline + y_top, baseline + y_bot)])
    return w, asc, desc


def _layout_nary(el, size, x, baseline, ctx, draw):
    """nary：求和/积分符号（手绘折线或大字形）+ 上下限 + 被作用式。"""
    nary_pr = _kid(el, "naryPr")
    chr_val = "\u2211"
    lim_loc = "undOvr"
    sub_hide = False
    sup_hide = False
    if nary_pr is not None:
        chr_val = _val_of(_kid(nary_pr, "chr"), "\u2211") or "\u2211"
        lim_loc = _val_of(_kid(nary_pr, "limLoc"), "undOvr") or "undOvr"
        sub_hide = _kid(nary_pr, "subHide") is not None
        sup_hide = _kid(nary_pr, "supHide") is not None

    e_el = _kid(el, "e")
    sub_el = _kid(el, "sub") if not sub_hide else None
    sup_el = _kid(el, "sup") if not sup_hide else None

    bw, ba, bd = _walk(e_el, size, 0.0, 0.0, ctx, False)
    ls = size * NARY_LIM_RATIO
    sw, sa, sd = (0.0, 0.0, 0.0)
    pw, pa, pd = (0.0, 0.0, 0.0)
    if sub_el is not None:
        sw, sa, sd = _walk(sub_el, ls, 0.0, 0.0, ctx, False)
    if sup_el is not None:
        pw, pa, pd = _walk(sup_el, ls, 0.0, 0.0, ctx, False)

    H = max(NARY_MIN_H * size, ba + bd)
    sym_w = NARY_SYM_W * size
    und = (lim_loc != "subSup")
    col_w = max(sym_w, pw, sw) if und else sym_w
    gap = NARY_GAP * size
    sym_top = baseline - NARY_TOP * H
    sym_bot = baseline + NARY_BOT * H

    if und:
        w = col_w + bw
        asc = max(ba, NARY_TOP * H + gap + pd + pa)
        desc = max(bd, NARY_BOT * H + gap + sa + sd)
    else:
        lim_w = max(pw, sw)
        w = col_w + lim_w + bw
        shift = SUBSUP_SHIFT * size
        asc = max(ba, NARY_TOP * H, shift + pa if sup_el is not None else 0.0,
                  max(0.0, sa - shift) if sub_el is not None else 0.0)
        desc = max(bd, NARY_BOT * H, shift + sd if sub_el is not None else 0.0,
                   max(0.0, pd - shift) if sup_el is not None else 0.0)

    if draw:
        # 符号
        sym_x = x + (col_w - sym_w) / 2.0
        polys = _nary_symbol(chr_val, sym_w, sym_top, sym_bot)
        if polys:
            for pts in polys:
                _draw_line(ctx, [(sym_x + px, py) for px, py in pts])
        elif ctx is not None:
            from layout import draw_char, char_advance_mm
            # 大字形：字身高贴合 H
            gsize = H / (TEXT_ASC + TEXT_DESC)
            adv = char_advance_mm(ctx, chr_val, gsize)
            draw_char(ctx, chr_val, sym_x, sym_bot - TEXT_DESC * gsize, gsize)
        # 上下限
        if und:
            if sup_el is not None:
                _walk(sup_el, ls, x + (col_w - pw) / 2.0,
                      baseline - NARY_TOP * H - gap - pd, ctx, True)
            if sub_el is not None:
                _walk(sub_el, ls, x + (col_w - sw) / 2.0,
                      baseline + NARY_BOT * H + gap + sa, ctx, True)
        else:
            lim_x = x + col_w
            if sup_el is not None:
                _walk(sup_el, ls, lim_x, baseline - SUBSUP_SHIFT * size, ctx, True)
            if sub_el is not None:
                _walk(sub_el, ls, lim_x, baseline + SUBSUP_SHIFT * size, ctx, True)
        # 被作用式
        if e_el is not None:
            bx = x + (col_w if und else col_w + max(pw, sw))
            _walk(e_el, size, bx, baseline, ctx, True)
    return w, asc, desc


# ---------------------------------------------------------------- 公共 API

def measure_math(el, size: float, ctx=None) -> tuple[float, float, float]:
    """m:oMath 元素 → (宽mm, 基线上方高mm, 基线下方深mm)。

    ctx 可选：给出时用 layout.char_advance_mm 精确度量（与 draw_math 一致），
    缺省按 em 估计。
    """
    if el is None or size <= 0:
        return 0.0, 0.0, 0.0
    return _walk(el, size, 0.0, 0.0, ctx, False)


def draw_math(ctx, el, x: float, baseline: float, size: float) -> float:
    """画进 ctx.strokes（复用 layout.draw_char/char_advance_mm），返回占用宽 mm。"""
    if el is None or size <= 0 or ctx is None:
        return 0.0
    w, _, _ = _walk(el, size, x, baseline, ctx, True)
    return w
