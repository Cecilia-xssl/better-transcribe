"""pdfconv.py —— PDF 文本抽取转 .docx（供 ensure_docx / 誊抄管线使用）。

文本行为不变；keep_drawings=True 时把每页矢量组（page.get_drawings()）转 SVG，
按 y 位置就近插入 docx 流（与最近文字块间隙 <6mm 归属，插在对应文字段落之间）。
插入方式：add_picture 1×1px PNG 占位后替换 image part 的 blob 为 SVG 字节；
blob 替换失败时降级为高对比 PNG（黑线白底，PIL 绘制）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 6mm 文字块归属阈值（pt）
_GAP_MM = 6.0
_GAP_PT = _GAP_MM * 72.0 / 25.4
# 插图宽 = 页宽 mm × 比例
_IMG_RATIO = 0.75

_PNG_SEQ = [0]


# ---------------------------------------------------------------- 矢量组 → SVG

def _group_svg(g: dict, rect) -> str:
    """一个 get_drawings() 矢量组 → SVG 字符串（M/L/C/Q/Z，描黑线，fill=none）。"""
    items = g.get("items") or []
    d_parts: list[str] = []
    cur = None
    eps = 1e-6

    def _need_move(p):
        nonlocal cur
        if cur is None or abs(cur[0] - p.x) > eps or abs(cur[1] - p.y) > eps:
            d_parts.append("M%.3f,%.3f" % (p.x, p.y))

    for it in items:
        op = it[0]
        if op == "l" and len(it) >= 3:
            p1, p2 = it[1], it[2]
            _need_move(p1)
            d_parts.append("L%.3f,%.3f" % (p2.x, p2.y))
            cur = (p2.x, p2.y)
        elif op == "c" and len(it) >= 5:
            p1, p2, p3, p4 = it[1], it[2], it[3], it[4]
            _need_move(p1)
            d_parts.append("C%.3f,%.3f %.3f,%.3f %.3f,%.3f"
                           % (p2.x, p2.y, p3.x, p3.y, p4.x, p4.y))
            cur = (p4.x, p4.y)
        elif op == "re" and len(it) >= 2:
            r = it[1]
            d_parts.append("M%.3f,%.3fL%.3f,%.3fL%.3f,%.3fL%.3f,%.3fZ"
                           % (r.x0, r.y0, r.x1, r.y0, r.x1, r.y1, r.x0, r.y1))
            cur = None
        elif op == "qu" and len(it) >= 2:
            q = it[1]
            pts = [q.ul, q.ur, q.lr, q.ll]
            d_parts.append("M" + "L".join("%.3f,%.3f" % (p.x, p.y) for p in pts) + "Z")
            cur = None
        else:
            continue
    if not d_parts:
        return ""
    # 有 fill / closePath 的画轮廓（收口）
    if g.get("closePath") or (g.get("type") or "") in ("f", "fs"):
        d_parts.append("Z")
    w = float(rect.width)
    h = float(rect.height)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.3f %.3f %.3f %.3f">'
            '<path d="%s" fill="none" stroke="black" stroke-width="1"/></svg>'
            % (float(rect.x0), float(rect.y0), w, h, " ".join(d_parts)))


def _page_drawings(page) -> list[tuple[str, tuple, tuple]]:
    """每组矢量 → (svg 字符串, 组 rect(x0,y0,x1,y1) pt, 页框 rect pt)。"""
    try:
        groups = page.get_drawings()
    except Exception:
        return []
    pr = page.rect
    page_box = (float(pr.x0), float(pr.y0), float(pr.x1), float(pr.y1))
    out = []
    for g in groups:
        r = g.get("rect") or pr
        svg = _group_svg(g, r)   # viewBox=组 rect：否则小线条撑成整页图
        if not svg:
            continue
        out.append((svg, (float(r.x0), float(r.y0), float(r.x1), float(r.y1)), page_box))
    return out


def page_drawings_svg(page) -> list[str]:
    """page.get_drawings() 的矢量组 → 每组一个 SVG 字符串（viewBox=页框 pt 尺寸）。
    path 用 M/L/C/Q/Z，描黑线 fill=none；有 fill 的画轮廓。无矢量 → []。
    """
    return [svg for svg, _, _ in _page_drawings(page)]


# ---------------------------------------------------------------- docx 插图

def _png_1x1_unique() -> bytes:
    """唯一的 1×1 PNG（python-docx 按 sha1 去重，必须逐个不同）。"""
    import io
    from PIL import Image
    i = _PNG_SEQ[0]
    _PNG_SEQ[0] = i + 1
    im = Image.new("RGB", (1, 1), (i & 255, (i >> 8) & 255, (i >> 16) & 255))
    bio = io.BytesIO()
    im.save(bio, format="PNG")
    return bio.getvalue()


def _svg_to_png(svg_bytes: bytes, w_mm: float, h_mm: float, dpi: int = 150) -> bytes:
    """矢量组 → 高对比 PNG（黑线白底）——blob 替换不可行时的降级路径。"""
    import io
    from PIL import Image, ImageDraw
    from img2stroke import svg_to_strokes
    scale = dpi / 25.4
    W = max(2, int(round(w_mm * scale)))
    H = max(2, int(round(h_mm * scale)))
    im = Image.new("L", (W, H), 255)
    dr = ImageDraw.Draw(im)
    for pts in svg_to_strokes(svg_bytes, w_mm, h_mm):
        xy = [(px * scale, py * scale) for px, py in pts]
        if len(xy) >= 2:
            dr.line(xy, fill=0, width=2)
    bio = io.BytesIO()
    im.save(bio, format="PNG")
    return bio.getvalue()


def _add_svg_picture(document, svg_bytes: bytes, gbox: tuple) -> str:
    """占位 PNG 插图后把 image part 的 blob 替换为 SVG 字节；失败降级 PNG。
    gbox=组 rect(x0,y0,x1,y1) pt：插图尺寸按组实际大小（曾误用页框——
    一条下划线被撑成整页大图把正文全挤掉）。返回 'svg' / 'png'。"""
    import io
    from docx.oxml.ns import qn
    from docx.shared import Mm

    x0, y0, x1, y1 = gbox
    w_pt = max(1.0, x1 - x0)
    h_pt = max(1.0, y1 - y0)
    img_w = w_pt * 25.4 / 72.0
    img_h = h_pt * 25.4 / 72.0
    # 等比 clamp：不放大；最大 150x100mm（限正文区内）
    scale = min(1.0, 150.0 / max(img_w, 0.1), 100.0 / max(img_h, 0.1))
    img_w = max(1.0, img_w * scale)
    img_h = max(0.5, img_h * scale)

    p = document.add_paragraph()
    run = p.add_run()
    run.add_picture(io.BytesIO(_png_1x1_unique()), width=Mm(img_w), height=Mm(img_h))
    blips = run._element.findall(".//" + qn("a:blip"))
    if not blips:
        return "png"
    rId = blips[0].get(qn("r:embed"))
    part = document.part.related_parts.get(rId)
    if part is None:
        return "png"
    try:
        part._blob = svg_bytes if isinstance(svg_bytes, bytes) else svg_bytes.encode("utf-8")
        return "svg"
    except Exception as e:
        print(f"  [pdfconv] blob 替换失败（降级 PNG）：{e}")
    try:
        part._blob = _svg_to_png(svg_bytes, img_w, img_h)
    except Exception as e:
        print(f"  [pdfconv] PNG 降级也失败（保留占位）：{e}")
    return "png"


def _assign_drawings(lines, draws) -> dict:
    """矢量组按 y 就近归属文字段落：{('before'|'after', idx): [(yc, svg), ...]}。
    与最近文字块间隙 <6mm 归属该块；否则按 y 插入对应位置。"""
    slots: dict = {}
    for svg, gbox in draws:
        dy0, dy1 = gbox[1], gbox[3]
        dc = (dy0 + dy1) / 2.0
        best = None
        for i, (yc, y0, y1, _spans) in enumerate(lines):
            if dy1 < y0:
                gap = y0 - dy1
            elif y1 < dy0:
                gap = dy0 - y1
            else:
                gap = 0.0
            if best is None or gap < best[0]:
                best = (gap, i, yc)
        if best is not None and best[0] < _GAP_PT:
            i = best[1]
            where = "after" if dc >= best[2] else "before"
            slots.setdefault((where, i), []).append((dc, svg, gbox))
        else:
            idx = len(lines)
            for j, (yc, _y0, _y1, _spans) in enumerate(lines):
                if yc > dc:
                    idx = j
                    break
            slots.setdefault(("before", idx), []).append((dc, svg, gbox))
    for key in slots:
        slots[key].sort(key=lambda t: t[0])
    return slots


# ---------------------------------------------------------------- 主入口

def pdf_to_docx(pdf_path, out_path=None, keep_drawings: bool = True) -> Path:
    """PDF 文本抽取转 .docx（文本行为不变）。

    keep_drawings=True 时把每页矢量组转 SVG 按 y 位置就近插入 docx 流
    （与最近文字块间隙 <6mm 归属；插在对应文字段落之间）。
    """
    import fitz
    from docx import Document
    from docx.shared import Pt

    src = Path(pdf_path)
    if not src.exists():
        raise FileNotFoundError(src)
    if src.suffix.lower() != ".pdf":
        raise ValueError(f"不是 PDF 文件：{src}")

    doc = fitz.open(str(src))
    pages_lines: list[list[tuple]] = []
    pages_draws: list[list[tuple]] = []
    page_boxes: list[tuple] = []
    for page in doc:
        d = page.get_text("dict")
        lines: list[tuple] = []
        for block in d.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                bb = line.get("bbox") or block.get("bbox") or (0.0, 0.0, 0.0, 0.0)
                y0, y1 = float(bb[1]), float(bb[3])
                lines.append(((y0 + y1) / 2.0, y0, y1,
                              [(sp.get("text", ""), float(sp.get("size", 12.0)))
                               for sp in spans]))
        pages_lines.append(lines)
        pr = page.rect
        page_boxes.append((float(pr.x0), float(pr.y0), float(pr.x1), float(pr.y1)))
        pages_draws.append(_page_drawings(page) if keep_drawings else [])
    doc.close()

    n_paras = sum(len(ln) for ln in pages_lines)
    if n_paras == 0:
        raise RuntimeError(f"PDF 无可提取文本（可能是纯图片扫描件）：{src}")

    out = Path(out_path) if out_path else src.with_suffix(".docx")
    document = Document()
    for pi, lines in enumerate(pages_lines):
        draws = [(svg, gbox) for svg, gbox, _pbox in pages_draws[pi]] if pi < len(pages_draws) else []
        slots = _assign_drawings(lines, draws) if draws else {}
        pbox = page_boxes[pi] if pi < len(page_boxes) else (0.0, 0.0, 595.0, 842.0)

        def _emit_pics(where: str, idx: int) -> None:
            items = slots.get((where, idx), [])
            if not items:
                return
            # 同槽多组聚合：联合 bbox + 拼 path，插一张图（否则 221 条线=221 张图）
            import re as _re
            ds = []
            bx0 = by0 = 1e18
            bx1 = by1 = -1e18
            for _dc, svg, gb in items:
                m = _re.search(r'd="([^"]*)"', svg)
                if m:
                    ds.append(m.group(1))
                bx0 = min(bx0, gb[0]); by0 = min(by0, gb[1])
                bx1 = max(bx1, gb[2]); by1 = max(by1, gb[3])
            bw, bh = max(bx1 - bx0, 1.0), max(by1 - by0, 1.0)
            merged = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.3f %.3f %.3f %.3f">'
                      '<path d="%s" fill="none" stroke="black" stroke-width="1"/></svg>'
                      % (bx0, by0, bw, bh, " ".join(ds)))
            _add_svg_picture(document, merged.encode("utf-8"), (bx0, by0, bx1, by1))

        for i, (_yc, _y0, _y1, spans) in enumerate(lines):
            _emit_pics("before", i)
            p = document.add_paragraph()
            for text, size in spans:
                if not text:
                    continue
                run = p.add_run(text)
                run.font.size = Pt(max(6.0, min(36.0, size)))
            _emit_pics("after", i)
        _emit_pics("before", len(lines))
    document.save(str(out))
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python pdfconv.py <in.pdf> [out.docx]", file=sys.stderr)
        sys.exit(1)
    in_pdf = sys.argv[1]
    out_docx = sys.argv[2] if len(sys.argv) > 2 else None
    result = pdf_to_docx(in_pdf, out_docx)
    from docx import Document as _Doc
    n = len(_Doc(str(result)).paragraphs)
    print(f"{result}：{n} 段")
