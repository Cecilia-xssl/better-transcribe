"""docx 排版引擎 —— 段落/表格/图片 → 页面 mm 坐标笔画。

只做与笔迹相关的版式：段落流式排版、对齐、缩进、字号、行距、表格线框与单元格文字、
图片（描摹轮廓）。页眉页脚/页码不做。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from gfont import EM_UNITS, GFont

PT2MM = 25.4 / 72.0
EMU2MM = 1.0 / 36000.0
# gfont 字身：顶 -450，基线 0，底 +21（单位）
EM_TOP = 450.0


@dataclass
class Stroke:
    points: list[tuple[float, float]]
    dwell_ms: float = 0.0   # 落笔后驻留毫秒（标记/轻点类笔画出墨用；普通书写 0）


@dataclass
class LayoutResult:
    width: float
    height: float
    strokes: list[Stroke] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    pages_used: int = 1
    truncated: bool = False
    pages: list[list[Stroke]] = field(default_factory=list)


class Ctx:
    """一次排版的共享状态。"""

    def __init__(self, font: GFont, page_w: float = 210.0, page_h: float = 297.0,
                 margin_l: float = 25.4, margin_r: float = 25.4,
                 margin_t: float = 25.4, margin_b: float = 25.4):
        self.font = font
        self.page_w = page_w
        self.page_h = page_h
        self.ml, self.mr = margin_l, margin_r
        self.mt, self.mb = margin_t, margin_b
        self.strokes: list[Stroke] = []
        self.missing: list[str] = []
        self.jitter_pct: float = 0.0   # 0~10，逐字随机偏移/缩放/旋转
        self._rng = None
        self.fallback = None  # HanziFallback | None
        self.draw_charts: bool = False  # 描摹图表类图（统计图表/CAD/SVG 线稿）
        self.draw_images: bool = False  # 描摹照片类图（素描化排线）

    @property
    def content_w(self) -> float:
        return self.page_w - self.ml - self.mr


# ---------- 字符度量与落笔 ----------

# 常见排版符号 → 可写字形（无对应字库时的归一化）
_CHAR_MAP = {
    "·": "•", "•": ".", "—": "-", "–": "-", "－": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
    "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁰": "0",
    "⁻": "-", "⁺": "+", "½": "1/2", "×": "x",
    "①": "(1)", "②": "(2)", "③": "(3)", "④": "(4)", "⑤": "(5)",
    "⑥": "(6)", "⑦": "(7)", "⑧": "(8)", "⑨": "(9)", "⑩": "(10)",
}


_ASCII_ALNUM = None  # 惰性编译
_PUNCT_SMALL = set(".,;:!?')]")


def adv_floor_units(ch: str) -> float:
    """步进下限（单位）：数字/字母 0.55em、半角标点 0.30em。纯包围盒宽度会挤叠重字。"""
    global _ASCII_ALNUM
    if _ASCII_ALNUM is None:
        import re as _re
        _ASCII_ALNUM = _re.compile(r"[A-Za-z0-9]")
    if _ASCII_ALNUM.match(ch):
        return EM_UNITS * 0.55
    if ch in _PUNCT_SMALL:
        return EM_UNITS * 0.30
    return 0.0


def adv_units(glyph, ch: str) -> float:
    return max(glyph.advance, adv_floor_units(ch))


def normalize_char(ch: str) -> str:
    return _CHAR_MAP.get(ch, ch)


def expand_chars(s: str) -> str:
    """文本级展开替换（支持一对多，如 ①→(1)），供排版前调用。"""
    return "".join(_CHAR_MAP.get(c, c) for c in s)


def _lookup(ctx: Ctx, ch: str):
    """gfont 优先，开源兜底。"""
    if ch in ctx.font:
        return ctx.font.glyph(ch)
    if ctx.fallback is not None and ch in ctx.fallback:
        return ctx.fallback.glyph(ch)
    return None


def char_advance_mm(ctx: Ctx, ch: str, size_mm: float) -> float:
    ch = normalize_char(ch)
    s = size_mm / EM_UNITS
    if ch == " ":
        return size_mm * 0.35
    g = _lookup(ctx, ch)
    if g is None:
        ctx.missing.append(ch)
        return size_mm * 0.9
    return adv_units(g, ch) * s


def draw_char(ctx: Ctx, ch: str, x: float, baseline_y: float, size_mm: float) -> float:
    """在基线位置画一个字，返回占用宽度 mm。"""
    import math
    import random

    s = size_mm / EM_UNITS
    ch = normalize_char(ch)
    adv = char_advance_mm(ctx, ch, size_mm)
    if ch == " ":
        return adv
    g = _lookup(ctx, ch)
    if g is None:
        return adv
    # 字身居中：把 [min_x, max_x] 映射到 [x, x+宽]
    off = -g.min_x * s
    j = ctx.jitter_pct / 100.0
    if j > 0:
        if ctx._rng is None:
            ctx._rng = random.Random(42)
        r = ctx._rng
        dx, dy = r.uniform(-j, j) * size_mm * 0.25, r.uniform(-j, j) * size_mm * 0.25
        k = 1.0 + r.uniform(-j, j) * 0.08
        rot = r.uniform(-j, j) * 0.06  # rad
    else:
        dx = dy = rot = 0.0
        k = 1.0
    cos_a, sin_a = math.cos(rot), math.sin(rot)
    for st in g.strokes:
        pts = []
        for px, py in st:
            ux = (off + px * s) * k
            uy = (py * s) * k
            rx = ux * cos_a - uy * sin_a
            ry = ux * sin_a + uy * cos_a
            pts.append((x + rx + dx, baseline_y + ry + dy))
        ctx.strokes.append(Stroke(pts))
    return adv


# ---------- 文本换行 ----------

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+[.,;:!?%]*|\s+|.", re.S)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def wrap_runs(runs: list[tuple[str, float]], width_mm: float, ctx: Ctx) -> list[list[tuple[str, float, float]]]:
    """runs: [(text, size_mm)] → 行列表，每行 [(ch, size_mm, x_offset)]。"""
    lines: list[list[tuple[str, float, float]]] = []
    cur: list[tuple[str, float, float]] = []
    cur_w = 0.0
    for text, size in runs:
        for tok in _tokens(expand_chars(text)):
            if tok == "\n":
                lines.append(cur)
                cur, cur_w = [], 0.0
                continue
            if tok == "\t":
                tok = "    "
            is_space = tok.isspace()
            if is_space and not cur:
                continue  # 行首空白丢弃
            advs = [char_advance_mm(ctx, c, size) for c in tok]
            tw = sum(advs)
            if cur_w + tw > width_mm and cur:
                # 词太长则逐字拆
                if tw > width_mm and not is_space:
                    for c, a in zip(tok, advs):
                        if cur_w + a > width_mm and cur:
                            lines.append(cur)
                            cur, cur_w = [], 0.0
                        cur.append((c, size, cur_w))
                        cur_w += a
                else:
                    lines.append(cur)
                    cur, cur_w = [], 0.0
                    if is_space:
                        continue
                    for c, a in zip(tok, advs):
                        cur.append((c, size, cur_w))
                        cur_w += a
            else:
                for c, a in zip(tok, advs):
                    cur.append((c, size, cur_w))
                    cur_w += a
    if cur:
        lines.append(cur)
    return lines


# ---------- 流式 token（文本/图片/形状/公式混排） ----------

def _image_polylines(ctx: Ctx, blob: bytes, w: float, h: float):
    """图片 → 折线（SVG 矢量/图表线稿/地图排线/照片素描）；不画返回 None。"""
    try:
        import img2stroke as i2s
        if i2s.is_svg(blob):
            if not ctx.draw_charts:
                return None
            return i2s.svg_to_strokes(blob, w, h)
        import io as _io
        from PIL import Image as _Image
        im = _Image.open(_io.BytesIO(blob))
        im.load()
        kind = i2s.classify_raster(im)
        if kind == "sketch":
            if not ctx.draw_images:
                return None
        elif not ctx.draw_charts:  # chart / map 同属线条图类
            return None
        return i2s.raster_to_strokes(im, w, h, kind)
    except Exception:
        return None


def _vml_style_box(el):
    """VML style 属性 → (w, h, left, top) mm；缺项为 None。"""
    st = el.get("style") or ""
    d = {}
    for part in st.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            d[k.strip().lower()] = v.strip()

    def mm(key):
        v = d.get(key)
        if not v:
            return None
        m = re.match(r"(-?[\d.]+)\s*(pt|mm|px|in)?", v)
        if not m:
            return None
        n = float(m.group(1))
        return {"pt": PT2MM, "mm": 1.0, "px": 25.4 / 96.0, "in": 25.4}.get(m.group(2) or "pt", PT2MM) * n

    return mm("width"), mm("height"), mm("left"), mm("top")


def _anchor_pos(anchor):
    """wp:anchor → 页面绝对 (x,y) mm；仅支持 relativeFrom=page，其余回落流式。"""
    def pos(tag):
        n = anchor.find(qn(tag))
        if n is None or n.get("relativeFrom") != "page":
            return None
        po = n.find(qn("wp:posOffset"))
        return int(po.text) * EMU2MM if po is not None and po.text else None

    x, y = pos("wp:positionH"), pos("wp:positionV")
    return (x, y) if x is not None and y is not None else None


def _atom(kind, w, h, ascent=None, descent=0.0, polylines=None, txbx=None, **extra):
    a = {"kind": kind, "w": w, "h": h,
         "ascent": h if ascent is None else ascent, "descent": descent,
         "polylines": polylines or [], "txbx": txbx or []}
    a.update(extra)
    return a


def _graphic_atom(ctx: Ctx, d, para, w: float, h: float):
    """原生图表（chart XML 部件）与 SmartArt（diagram 数据部件）→ atom 或 None。"""
    for e in d.iter():
        if not e.tag.endswith("}graphicData"):
            continue
        uri = (e.get("uri") or "").lower()
        if uri.endswith("/chart"):
            rid = None
            for ch in e.iter():
                if ch.tag.endswith("}chart"):
                    rid = ch.get(qn("r:id"))
                    break
            part = para.part.related_parts.get(rid) if rid else None
            return _atom("chart", w, h, data=part.blob) if part is not None else None
        if uri.endswith("diagram"):
            rid = None
            for ch in e.iter():
                if ch.tag.endswith("}relIds"):
                    rid = ch.get(qn("r:dm"))
                    break
            part = para.part.related_parts.get(rid) if rid else None
            if part is None:
                return None
            try:
                import mindmap
                return _atom("mindmap", w, h, tree=mindmap.parse_smartart(part.blob))
            except Exception:
                ctx.missing.append("[SmartArt 跳过]")
                return None
    return None


def _ole_atom(ctx: Ctx, el, para, max_w=None):
    """w:object → 内嵌 Excel 工作表原子；无 xlsx 时用预览图兜底。"""
    shape = None
    for e in el.iter():
        if e.tag.endswith("}shape"):
            shape = e
            break
    w, h = _vml_style_box(shape)[:2] if shape is not None else (None, None)
    w, h = _fit_box(w or 80.0, h or 45.0, max_w)
    ole_rid, prog = None, ""
    for e in el.iter():
        if e.tag.endswith("}OLEObject"):
            ole_rid = e.get(qn("r:id"))
            prog = (e.get("ProgID") or "").lower()
            break
    if ole_rid and "excel" in prog:
        part = para.part.related_parts.get(ole_rid)
        if part is not None:
            try:
                import sheetconv
                rows = sheetconv.rows_from_xlsx(part.blob)
                if rows:
                    hh = sheetconv.estimate_sheet_h(rows, w)
                    return ("atom", _atom("sheet", w, hh, rows=rows))
            except Exception as e:
                ctx.missing.append(f"[内嵌表格提取失败 {e}]")
    imd = None
    for e in el.iter():
        if e.tag.endswith("}imagedata"):
            imd = e
            break
    if imd is not None:
        rid = imd.get(qn("r:id"))
        part = para.part.related_parts.get(rid) if rid else None
        if part is not None:
            polys = _image_polylines(ctx, part.blob, w, h)
            if polys is None:
                ctx.missing.append(f"[图片留白 {w:.0f}x{h:.0f}mm]")
            return ("atom", _atom("image", w, h, polylines=polys or []))
    return None


def _fit_box(w: float, h: float, max_w):
    if max_w and w > max_w:
        h = h * max_w / w
        w = max_w
    return w, h


def _drawing_atom(ctx: Ctx, d, para, max_w=None):
    """w:drawing（wp:inline/wp:anchor 内 wps:wsp 或 a:blip）→ token 或 None。"""
    inline = d.find(".//" + qn("wp:inline"))
    anchor = d.find(".//" + qn("wp:anchor"))
    node = inline if inline is not None else anchor
    if node is None:
        return None
    ext = node.find(qn("wp:extent"))
    if ext is not None:
        w, h = int(ext.get("cx")) * EMU2MM, int(ext.get("cy")) * EMU2MM
    else:
        w, h = 60.0, 45.0
    txbx = []
    for tc in d.findall(".//" + qn("w:txbxContent")):
        txbx.extend(tc.findall(qn("w:p")))
    # 原生图表 / SmartArt：数据部件（非图片非形状）
    g = _graphic_atom(ctx, d, para, w, h)
    if g is not None:
        if anchor is not None:
            pos = _anchor_pos(anchor)
            if pos:
                g["x"], g["y"] = pos
                return ("anchor", g)
        return ("atom", g)
    blip = d.find(".//" + qn("a:blip"))
    if blip is not None:
        rid = blip.get(qn("r:embed"))
        for e in blip.iter():
            if e.tag.endswith("}svgBlip"):
                rid = e.get(qn("r:embed")) or rid
        part = para.part.related_parts.get(rid) if rid else None
        if part is None:
            return None
        w, h = _fit_box(w, h, max_w)
        polys = _image_polylines(ctx, part.blob, w, h)
        if polys is None:
            ctx.missing.append(f"[图片留白 {w:.0f}x{h:.0f}mm]")
        atom = _atom("image", w, h, polylines=polys or [], txbx=txbx)
    else:
        sh = None
        try:
            import drawml
            sh = drawml.parse_shape(d)
        except Exception:
            sh = None
        if sh:
            if sh.get("w"):
                w, h = float(sh["w"]), float(sh["h"])
            atom = _atom("shape", w, h, polylines=sh.get("polylines") or [], txbx=sh.get("txbx") or txbx)
            if sh.get("x") is not None and sh.get("y") is not None:
                atom["x"], atom["y"] = float(sh["x"]), float(sh["y"])
                return ("anchor", atom)
        elif txbx:
            atom = _atom("shape", w, h, polylines=[], txbx=txbx)  # 至少写框内文字
        else:
            return None
    if anchor is not None:
        pos = _anchor_pos(anchor)
        if pos:
            atom["x"], atom["y"] = pos
            return ("anchor", atom)
    return ("atom", atom)


def _vml_atom(ctx: Ctx, pict, para, max_w=None):
    """w:pict（VML v:shape / v:imagedata）→ token 或 None。"""
    w, h, left, top = _vml_style_box(pict)
    w, h = _fit_box(w or 60.0, h or 45.0, max_w)
    imd = pict.find(".//" + qn("v:imagedata"))
    if imd is not None:
        rid = imd.get(qn("r:id"))
        part = para.part.related_parts.get(rid) if rid else None
        if part is None:
            return None
        polys = _image_polylines(ctx, part.blob, w, h)
        if polys is None:
            ctx.missing.append(f"[图片留白 {w:.0f}x{h:.0f}mm]")
        atom = _atom("image", w, h, polylines=polys or [])
    else:
        sh = None
        try:
            import drawml
            sh = drawml.parse_shape(pict)
        except Exception:
            sh = None
        if not sh:
            return None
        atom = _atom("shape", w, h, polylines=sh.get("polylines") or [],
                     txbx=sh.get("txbx") or [])
        if sh.get("w"):
            atom["w"], atom["h"], atom["ascent"] = float(sh["w"]), float(sh["h"]), float(sh["h"])
    if left is not None and top is not None:
        atom["x"], atom["y"] = left, top
        return ("anchor", atom)
    return ("atom", atom)


def _emit(tok, flow, anchors):
    if tok is None:
        return
    (anchors if tok[0] == "anchor" else flow).append(tok)


def _collect_run_tokens(ctx: Ctx, r_el, para, default_pt: float, flow, anchors, max_w=None):
    size = _run_size_mm(Run(r_el, para), default_pt)
    buf: list[str] = []

    def flush_text():
        if buf:
            flow.append(("text", "".join(buf), size))
            buf.clear()

    for child in r_el:
        tag = child.tag.split("}")[-1]
        if tag == "t":
            buf.append(child.text or "")
        elif tag == "tab":
            buf.append("\t")
        elif tag in ("br", "cr"):
            buf.append("\n")
        elif tag == "drawing":
            flush_text()
            _emit(_drawing_atom(ctx, child, para, max_w), flow, anchors)
        elif tag == "pict":
            flush_text()
            _emit(_vml_atom(ctx, child, para, max_w), flow, anchors)
        elif tag == "object":
            flush_text()
            _emit(_ole_atom(ctx, child, para, max_w), flow, anchors)
    flush_text()


_INLINE_WRAP = {"hyperlink", "ins", "smartTag", "fldSimple", "sdt", "sdtContent"}


def _walk_inline(el, seq):
    for child in el:
        tag = child.tag.split("}")[-1]
        if tag == "r":
            seq.append(("r", child))
        elif tag in ("oMath", "oMathPara", "drawing", "pict"):
            seq.append((tag, child))
        elif tag in _INLINE_WRAP:
            _walk_inline(child, seq)  # 递归保序（超链接/修订/域里的 run 不丢）


def _para_tokens(ctx: Ctx, para: Paragraph, default_pt: float, max_w=None):
    """段落 → (流式 tokens, 绝对定位 tokens)。token: ('text',s,size)|('atom',atom)|('anchor',atom)"""
    flow: list = []
    anchors: list = []
    seq: list = []
    _walk_inline(para._p, seq)
    for kind, el in seq:
        if kind == "r":
            _collect_run_tokens(ctx, el, para, default_pt, flow, anchors, max_w)
        elif kind in ("oMath", "oMathPara"):
            for sub in ([el] if kind == "oMath" else el.findall(qn("m:oMath"))):
                try:
                    import ommath
                    size = default_pt * PT2MM
                    w, asc, desc = ommath.measure_math(sub, size)
                    flow.append(("atom", _atom("math", w, asc + desc, ascent=asc,
                                               descent=desc)))
                    flow[-1][1]["el"] = sub
                    flow[-1][1]["size"] = size
                except Exception:
                    ctx.missing.append("[公式跳过]")
        elif kind == "drawing":
            _emit(_drawing_atom(ctx, el, para, max_w), flow, anchors)
        elif kind == "pict":
            _emit(_vml_atom(ctx, el, para, max_w), flow, anchors)
    return flow, anchors


def _flow_lines(ctx: Ctx, tokens, width_mm: float, lf: float):
    """混排流 → 行列表：{items,width,ascent,advance}；item=('c',c,size,off)|('atom',atom,off)"""
    lines: list[dict] = []
    items: list = []
    cur_w = 0.0

    def flush():
        nonlocal items, cur_w
        lines.append({"items": items, "width": cur_w})
        items, cur_w = [], 0.0

    def put_char(c, size, adv):
        nonlocal cur_w
        items.append(("c", c, size, cur_w))
        cur_w += adv

    for tok in tokens:
        if tok[0] == "atom":
            a, aw = tok[1], tok[1]["w"]
            if cur_w + aw > width_mm and items:
                flush()
            items.append(("atom", a, None, cur_w))
            cur_w += aw
            continue
        _, text, size = tok
        for seg_i, seg in enumerate(text.split("\n")):
            if seg_i:
                flush()
            for t in _tokens(expand_chars(seg)):
                if t == "\t":
                    t = "    "
                is_space = t.isspace()
                if is_space and not items:
                    continue
                advs = [char_advance_mm(ctx, c, size) for c in t]
                tw = sum(advs)
                if cur_w + tw > width_mm and items:
                    if tw > width_mm and not is_space:
                        for c, a in zip(t, advs):
                            if cur_w + a > width_mm and items:
                                flush()
                            put_char(c, size, a)
                    else:
                        flush()
                        if is_space:
                            continue
                        for c, a in zip(t, advs):
                            put_char(c, size, a)
                else:
                    for c, a in zip(t, advs):
                        put_char(c, size, a)
    if items:
        flush()

    out = []
    for ln in lines:
        asc = desc = 0.0
        for it in ln["items"]:
            if it[0] == "c":
                sz = it[2]
                a = EM_TOP * sz / EM_UNITS
                asc, desc = max(asc, a), max(desc, sz * lf - a)
            else:
                at = it[1]
                asc = max(asc, at.get("ascent", at["h"]))
                desc = max(desc, at.get("descent", 0.0))
        ln["ascent"], ln["advance"] = asc, asc + desc
        out.append(ln)
    return out


def _draw_atom(ctx: Ctx, atom, x: float, baseline: float):
    y_top = baseline - atom.get("ascent", atom["h"])
    kind = atom.get("kind")
    if kind == "math":
        try:
            import ommath
            ommath.draw_math(ctx, atom["el"], x, baseline, atom.get("size", 4.0))
        except Exception:
            pass
    elif kind == "chart":
        try:
            import chartxml
            chartxml.draw_chart(ctx, atom["data"], x, y_top, atom["w"], atom["h"])
        except Exception:
            ctx.missing.append("[图表绘制失败]")
    elif kind == "mindmap":
        try:
            import mindmap
            mindmap.draw_tree(ctx, atom["tree"], x, y_top, atom["w"], atom["h"])
        except Exception:
            ctx.missing.append("[结构图绘制失败]")
    elif kind == "sheet":
        try:
            import sheetconv
            sheetconv.draw_sheet(ctx, atom["rows"], x, y_top, atom["w"])
        except Exception:
            ctx.missing.append("[表格绘制失败]")
    for pts in atom.get("polylines") or []:
        if len(pts) >= 2:
            ctx.strokes.append(Stroke([(x + px, y_top + py) for px, py in pts]))
    for p_el in atom.get("txbx") or []:
        try:
            inner = Paragraph(p_el, atom.get("parent") or None)
            layout_paragraph(ctx, inner, y_top + 1.0, x + 1.0, max(atom["w"] - 2.0, 5.0))
        except Exception:
            pass


# ---------- 段落 ----------

def _run_size_mm(run, default_pt: float = 12.0) -> float:
    pt = run.font.size.pt if run.font.size is not None else default_pt
    return pt * PT2MM


def estimate_para_height(ctx: Ctx, para: Paragraph, box_w: float,
                         default_pt: float = 12.0, line_factor: float = 1.6) -> float:
    """预估段落高度（用于整段翻页，防段落跨页被裁）。"""
    pf = para.paragraph_format
    sb = (pf.space_before.pt * PT2MM if pf.space_before else 0.0)
    sa = (pf.space_after.pt * PT2MM if pf.space_after else 0.0)
    if pf.line_spacing is not None:
        lf = pf.line_spacing if isinstance(pf.line_spacing, float) else 1.6
    else:
        lf = line_factor
    indent_l = (pf.left_indent.mm if pf.left_indent else 0.0)
    indent_f = (pf.first_line_indent.mm if pf.first_line_indent else 0.0)
    flow, _ = _para_tokens(ctx, para, default_pt, box_w)
    if not flow:
        return default_pt * PT2MM * lf + sb + sa
    lines = _flow_lines(ctx, flow, max(box_w - indent_l - indent_f, 10.0), lf)
    return sb + sa + sum(ln["advance"] for ln in lines if ln["items"])


def layout_paragraph(ctx: Ctx, para: Paragraph, y: float, box_l: float, box_w: float,
                     default_pt: float = 12.0, line_factor: float = 1.6) -> float:
    """排一个段落，返回 y 光标（mm）。文本/图片/形状/公式混排；锚定浮动按绝对坐标画。"""
    pf = para.paragraph_format
    align = para.alignment  # None=继承(左)
    indent_l = (pf.left_indent.mm if pf.left_indent else 0.0)
    indent_f = (pf.first_line_indent.mm if pf.first_line_indent else 0.0)
    space_before = (pf.space_before.pt * PT2MM if pf.space_before else 0.0)
    space_after = (pf.space_after.pt * PT2MM if pf.space_after else 0.0)
    if pf.line_spacing is not None:
        lf = pf.line_spacing if isinstance(pf.line_spacing, float) else 1.6
    else:
        lf = line_factor

    flow, anchors = _para_tokens(ctx, para, default_pt, box_w)
    for _, a in anchors:
        _draw_atom(ctx, a, a.get("x", box_l), a.get("y", y) + a.get("ascent", a["h"]))
    if not flow:
        return y + default_pt * PT2MM * lf

    y += space_before
    text_w = box_w - indent_l - indent_f
    lines = _flow_lines(ctx, flow, max(text_w, 10.0), lf)
    for li, ln in enumerate(lines):
        if not ln["items"]:
            continue
        line_w = ln["width"]
        # 对齐
        avail = box_w - indent_l
        if align == WD_ALIGN_PARAGRAPH.CENTER:
            x0 = box_l + indent_l + max(0.0, (avail - line_w) / 2)
        elif align == WD_ALIGN_PARAGRAPH.RIGHT:
            x0 = box_l + indent_l + max(0.0, avail - line_w)
        else:
            x0 = box_l + indent_l
        if li == 0:
            x0 += indent_f
        baseline = y + ln["ascent"]
        for it in ln["items"]:
            if it[0] == "c":
                draw_char(ctx, it[1], x0 + it[3], baseline, it[2])
            else:
                _draw_atom(ctx, it[1], x0 + it[3], baseline)
        y += ln["advance"]
    return y + space_after


# ---------- 表格 ----------

def layout_table(ctx: Ctx, tbl: Table, y: float, box_l: float, box_w: float,
                 default_pt: float = 11.0) -> float:
    rows = tbl.rows
    n_cols = len(rows[0].cells) if rows else 0
    if n_cols == 0:
        return y
    # 列宽：优先 tblGrid
    col_ws: list[float] = []
    try:
        for gc in tbl._tbl.tblGrid.gridCol_lst:
            col_ws.append(gc.w.mm)
        if len(col_ws) != n_cols:
            raise ValueError
    except Exception:
        col_ws = [box_w / n_cols] * n_cols
    pad = 0.8  # 单元格内边距 mm（紧凑）
    min_sz = 3.2  # 表格字号下限 mm——量测/换行/绘制必须同源取此值，否则文字比格子宽、压线重叠

    def csz(r) -> float:
        return max(_run_size_mm(r, default_pt), min_sz)

    # 列宽适配内容：按列内容实际所需宽度压缩/放大，避免表格撑满浪费空间
    need = []
    for ci in range(n_cols):
        w_need = 2 * pad
        for row in rows:
            cell = row.cells[ci]
            for pp in cell.paragraphs:
                w_line = 0.0
                for r in pp.runs:
                    if r.text:
                        sz = csz(r)
                        w_line += sum(char_advance_mm(ctx, c, sz) for c in expand_chars(r.text))
                w_need = max(w_need, w_line + 2 * pad)
        need.append(w_need)
    tot = sum(need) or 1.0
    if tot < box_w:
        col_ws = need                       # 内容窄：不撑满，右侧留白
    else:
        col_ws = [w * box_w / tot for w in need]
        # 挤压下限=一个字号下限字符宽；内容本身更窄才允许更窄（防极端挤扁致逐字竖排）
        floor = 2 * pad + min_sz
        col_ws = [max(c, min(n, floor)) for c, n in zip(col_ws, need)]

    # 先量全部行（行高 + 各格折行）
    row_data: list[tuple[float, list[list[list[tuple[str, float, float]]]]]] = []
    for row in rows:
        cell_lines: list[list[list[tuple[str, float, float]]]] = []
        row_h = default_pt * PT2MM * 1.6 + 2 * pad
        for ci in range(n_cols):
            cell = row.cells[ci]
            all_lines: list[list[tuple[str, float, float]]] = []
            for p in cell.paragraphs:
                runs = [(r.text, csz(r)) for r in p.runs if r.text]
                if not runs:
                    continue
                # 换行宽度=格子实际内宽（+0.05 余量吸收浮点误差，防恰好等宽被折行；
                # 不许大于内宽，否则文字压线）
                lines = wrap_runs(runs, max(col_ws[ci] - 2 * pad, 1.0) + 0.05, ctx)
                all_lines.extend(lines)
            cell_lines.append(all_lines)
            h = sum(max((sz for _, sz, _ in ln), default=default_pt * PT2MM) * 1.3 for ln in all_lines) + 2 * pad
            row_h = max(row_h, h)
        row_data.append((row_h, cell_lines))

    # 网格线只画唯一横竖线（不逐格画方框：省时，且共享边不重复描、减少拖纸）
    ys = [y]
    for row_h, _ in row_data:
        ys.append(ys[-1] + row_h)
    xs = [box_l]
    for w in col_ws:
        xs.append(xs[-1] + w)
    for yy in ys:
        ctx.strokes.append(Stroke([(xs[0], yy), (xs[-1], yy)]))
    for xx in xs:
        ctx.strokes.append(Stroke([(xx, ys[0]), (xx, ys[-1])]))

    # 文字
    yy = y
    for row_h, cell_lines in row_data:
        x = box_l
        for ci in range(n_cols):
            ty = yy + pad
            for ln in cell_lines[ci]:
                if not ln:
                    continue
                max_size = max(sz for _, sz, _ in ln)
                baseline = ty + EM_TOP * (max_size / EM_UNITS)
                for c, sz, off in ln:
                    draw_char(ctx, c, x + pad + off, baseline, sz)
                ty += max_size * 1.3
            x += col_ws[ci]
        yy += row_h
    return ys[-1]



# ---------- 入口 ----------

def _hf_text(doc) -> tuple[str, str]:
    """提取页眉/页脚文本（各 section 的 header/footer 合并去重）。"""
    hs: list[str] = []
    fs: list[str] = []
    for sec in doc.sections:
        for part in (sec.header, sec.first_page_header, sec.even_page_header):
            try:
                for p in part.paragraphs:
                    if p.text.strip():
                        hs.append(p.text.strip())
            except Exception:
                pass
        for part in (sec.footer, sec.first_page_footer, sec.even_page_footer):
            try:
                for p in part.paragraphs:
                    if p.text.strip():
                        fs.append(p.text.strip())
            except Exception:
                pass
    return " ".join(dict.fromkeys(hs)), " ".join(dict.fromkeys(fs))


def _draw_centered(ctx: Ctx, text: str, cx: float, baseline: float, size: float) -> None:
    text = expand_chars(text)
    w = sum(char_advance_mm(ctx, normalize_char(c), size) for c in text)
    x = cx - w / 2.0
    for c in text:
        x += draw_char(ctx, c, x, baseline, size)


def _draw_hf(ctx: Ctx, box: tuple[float, float, float, float],
             htxt: str, ftxt: str) -> None:
    """页眉画在区域上方居中，页脚画在区域下方居中。"""
    bx, by, bw, bh = box
    cx = bx + bw / 2.0
    if htxt:
        _draw_centered(ctx, htxt, cx, by - 4.0, 3.2)
    if ftxt:
        _draw_centered(ctx, ftxt, cx, by + bh + 6.0, 3.2)


def layout_docx(docx_path: str | Path, font: GFont, ctx: Ctx | None = None,
                potrace_exe: Path | None = None, max_pages: int = 1,
                boxes: list[tuple[float, float, float, float]] | None = None,
                skip_first_para: bool = False, with_headers: bool = True) -> LayoutResult:
    """boxes: 逐页书写区域 (x, y, w, h) 列表（同一纸面坐标系）；None=按 ctx 边距单页。

    第 1 区域（如表纸正文区）写满后进入下一区域（换纸页），页与页之间由调用方插换纸暂停。
    """
    ctx = ctx or Ctx(font)
    doc = Document(str(docx_path))
    if boxes is None:
        boxes = [(ctx.ml, ctx.mt, ctx.content_w, ctx.page_h - ctx.mt - ctx.mb)]
    boxes = list(boxes)[:max_pages]
    page_no = 0
    truncated = False
    bx, by, bw, bh = boxes[0]
    y = by
    marks = [len(ctx.strokes)]  # ctx.strokes 中各页起始下标（相对本次调用，ctx 可能已累积内容）
    htxt, ftxt = ("", "")
    if with_headers:
        htxt, ftxt = _hf_text(doc)
        _draw_hf(ctx, boxes[0], htxt, ftxt)

    def overflow(force: bool = False) -> bool:
        """超出当前区域时翻到下一区域；无后续区域则截断。"""
        nonlocal y, page_no, truncated, bx, by, bw, bh
        if not force and y <= by + bh:
            return False
        if page_no >= len(boxes) - 1:
            truncated = True
            return True
        page_no += 1
        bx, by, bw, bh = boxes[page_no]
        y = by
        marks.append(len(ctx.strokes))
        if with_headers:
            _draw_hf(ctx, boxes[page_no], htxt, ftxt)
        return False

    body = doc.element.body
    from docx.oxml.ns import qn

    para_seen = 0
    for child in body.iterchildren():
        if truncated:
            break
        if child.tag == qn("w:p"):
            if skip_first_para and para_seen == 0 and Paragraph(child, doc).text.strip():
                para_seen += 1
                continue  # 跳过首段（标题，表头题目栏已写）
            para_seen += 1
        # 块前预检：剩余空间不足一行则先翻页
        if y + 10.0 > by + bh:
            if overflow():
                break
        if child.tag == qn("w:p"):
            para = Paragraph(child, doc)
            # 文本/图片/形状/公式统一走段落 token 流（图片按行内原子随文排，超宽自动缩）
            est = estimate_para_height(ctx, para, bw)
            if y + est > by + bh:
                if overflow(force=True):
                    break
            y = layout_paragraph(ctx, para, y, bx, bw)
            overflow()
        elif child.tag == qn("w:tbl"):
            tbl = Table(child, doc)
            # 表格整体预估高度（保守），放不下先翻页
            est_h = len(tbl.rows) * 14.0 + 10.0
            if y + est_h > by + bh:
                if overflow(force=True):
                    break
            y = layout_table(ctx, tbl, y, bx, bw)
            overflow()

    # 拆页 → 逐页纸面裁剪（超出页底/页外的笔画丢弃，防悬空/撞机）
    marks.append(len(ctx.strokes))
    pages: list[list[Stroke]] = []
    for i in range(len(marks) - 1):
        seg = ctx.strokes[marks[i] : marks[i + 1]]
        clipped: list[Stroke] = []
        for st in seg:
            pts = [(x, yy) for x, yy in st.points
                   if -200.0 <= x <= ctx.page_w + 1.0 and yy <= ctx.page_h + 1.0]
            if len(pts) >= 2:
                clipped.append(Stroke(pts))
            elif len(st.points) != len(pts):
                truncated = True
        if len(clipped) != len(seg):
            truncated = True
        pages.append(clipped)
    all_strokes = [st for p in pages for st in p]

    return LayoutResult(width=ctx.page_w, height=ctx.page_h,
                        strokes=all_strokes, missing=sorted(set(ctx.missing)),
                        pages_used=page_no + 1, truncated=truncated, pages=pages)
