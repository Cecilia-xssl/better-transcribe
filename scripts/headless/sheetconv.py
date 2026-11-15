"""sheetconv.py —— Excel 表格：xlsx 读值 / 转 docx / 手绘表格渲染。

draw_sheet 复用 layout.layout_table 的紧凑表格渲染（列宽自适应/网格线/字号下限）：
在内存中用 python-docx 构造含表 Document 后交给 layout_table 落笔。
estimate_sheet_h 用同一路径在“丢弃 ctx”（测量假字库）上量高，保证与实绘同公式。
"""

from __future__ import annotations

import io
from pathlib import Path


# gfont 字身单位（与 gfont.EM_UNITS 同源，避免模块级 import layout）
_EM_UNITS = 470.0
_ASCII_ALNUM = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_ASCII_SMALL = set(".,;:!?')]")


class _MeasGlyph:
    """测量用假字形：只提供 advance/min_x/strokes（不落笔）。"""

    __slots__ = ("advance", "min_x", "strokes")

    def __init__(self, adv: float):
        self.advance = adv
        self.min_x = 0.0
        self.strokes: list = []


class _MeasFont:
    """测量用假字库：CJK 1em、ASCII 字母数字 0.55em、半角小标点 0.30em（近似 layout 步进）。"""

    def __contains__(self, ch: str) -> bool:
        return True

    def glyph(self, ch: str) -> _MeasGlyph:
        if ch in _ASCII_ALNUM:
            return _MeasGlyph(_EM_UNITS * 0.55)
        if ch in _ASCII_SMALL:
            return _MeasGlyph(_EM_UNITS * 0.30)
        return _MeasGlyph(_EM_UNITS)


# ---------------------------------------------------------------- xlsx 读值

def _fmt_num(v) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return str(v)
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))  # 12.0 → "12"
        s = f"{v:.10f}".rstrip("0").rstrip(".")  # 12.50 → "12.5"
        return s if s else "0"
    return str(v)


def _cell_text(ws_v, ws_f, r: int, c: int) -> str:
    v = ws_v.cell(row=r, column=c).value
    if v is None:
        if ws_f is not None:
            fv = ws_f.cell(row=r, column=c).value
            if isinstance(fv, str) and fv.startswith("="):
                return fv  # 公式无缓存值 → 显示公式串
        return ""
    return _fmt_num(v)


def _sheet_rows(ws_v, ws_f) -> list[list[str]]:
    rows: list[list[str]] = []
    for r in range(1, (ws_v.max_row or 0) + 1):
        row = []
        for c in range(1, (ws_v.max_column or 0) + 1):
            row.append(_cell_text(ws_v, ws_f, r, c))
        rows.append(row)
    return rows


def _load_pair(src):
    from openpyxl import load_workbook
    if isinstance(src, (bytes, bytearray)):
        raw = bytes(src)
        wb_v = load_workbook(io.BytesIO(raw), data_only=True)
        wb_f = load_workbook(io.BytesIO(raw), data_only=False)
    else:
        p = str(src)
        wb_v = load_workbook(p, data_only=True)
        wb_f = load_workbook(p, data_only=False)
    return wb_v, wb_f


def _first_sheet_pair(wb_v, wb_f):
    ws_v = wb_v.active if wb_v.active is not None else wb_v.worksheets[0]
    idx = wb_v.worksheets.index(ws_v)
    ws_f = wb_f.worksheets[idx] if idx < len(wb_f.worksheets) else None
    return ws_v, ws_f


def rows_from_xlsx(src) -> list[list[str]]:
    """xlsx 路径或 bytes → 第一个活动 sheet 的单元格文本矩阵（openpyxl 读值；
    数字 trim 尾 0；None→''；公式取缓存值 data_only=True，无缓存则显示公式串）。"""
    wb_v, wb_f = _load_pair(src)
    try:
        ws_v, ws_f = _first_sheet_pair(wb_v, wb_f)
        return _sheet_rows(ws_v, ws_f)
    finally:
        wb_v.close()
        wb_f.close()


# ---------------------------------------------------------------- xlsx → docx

def xlsx_to_docx(xlsx_path, out_path=None) -> Path:
    """xlsx → docx：每个 sheet 一个加粗标题段 + 一个真实表格（python-docx 生成；
    首行加粗作表头；列数/行数不限；返回输出路径。"""
    from docx import Document

    src = Path(xlsx_path)
    if not src.exists():
        raise FileNotFoundError(src)
    out = Path(out_path) if out_path else src.with_suffix(".docx")
    wb_v, wb_f = _load_pair(src)
    try:
        doc = Document()
        for i, ws_v in enumerate(wb_v.worksheets):
            ws_f = wb_f.worksheets[i] if i < len(wb_f.worksheets) else None
            rows = _sheet_rows(ws_v, ws_f)
            title = doc.add_paragraph()
            run = title.add_run(str(ws_v.title))
            run.bold = True
            ncols = max((len(r) for r in rows), default=0)
            if not rows or ncols == 0:
                continue
            table = doc.add_table(rows=len(rows), cols=ncols)
            try:
                table.style = doc.styles["Table Grid"]
            except Exception:
                pass
            for ri, row in enumerate(rows):
                for ci in range(ncols):
                    cell = table.cell(ri, ci)
                    run = cell.paragraphs[0].add_run(row[ci] if ci < len(row) else "")
                    if ri == 0:
                        run.bold = True
        doc.save(str(out))
    finally:
        wb_v.close()
        wb_f.close()
    return out


# ---------------------------------------------------------------- 手绘表格

def _norm_rows(rows) -> list[list[str]]:
    if not rows:
        return []
    ncols = max((len(r) for r in rows), default=0)
    if ncols == 0:
        return []
    out = []
    for r in rows:
        rr = []
        for i in range(ncols):
            v = r[i] if i < len(r) else ""
            rr.append("" if v is None else str(v))
        out.append(rr)
    return out


def _table_doc(norm: list[list[str]]):
    """在内存中构造只含一个表格的 Document（draw_sheet / estimate_sheet_h 共用）。"""
    from docx import Document
    doc = Document()
    ncols = len(norm[0])
    table = doc.add_table(rows=len(norm), cols=ncols)
    try:
        table.style = doc.styles["Table Grid"]
    except Exception:
        pass
    for ri, row in enumerate(norm):
        for ci in range(ncols):
            table.cell(ri, ci).paragraphs[0].add_run(row[ci])
    return doc


def estimate_sheet_h(rows: list[list[str]], w: float, default_pt: float = 11.0) -> float:
    """预估绘制高度 mm（与 draw_sheet 的实际高度同公式，供流程排版占位）。"""
    norm = _norm_rows(rows)
    if not norm:
        return 0.0
    from layout import Ctx, layout_table
    doc = _table_doc(norm)
    ctx = Ctx(_MeasFont())  # 丢弃 ctx：测量假字库，不产生可见笔画
    return layout_table(ctx, doc.tables[0], 0.0, 0.0, w, default_pt)


def draw_sheet(ctx, rows: list[list[str]], x: float, y: float, w: float,
               default_pt: float = 11.0) -> float:
    """行矩阵 → 手绘表格（画进 ctx.strokes，返回实际高度 mm）。
    实现要求：在内存中用 python-docx 构造含表的 Document，再调用
    `from layout import layout_table` 复用现有紧凑表格渲染（列宽自适应/网格线/字号下限），
    即：draw_sheet = 构造 Table → layout_table(ctx, doc.tables[0], y, x, w)。
    estimate_sheet_h 用同参数先在丢弃 ctx 上跑一遍量高（或用相同行高公式计算）。"""
    norm = _norm_rows(rows)
    if not norm:
        return 0.0
    from layout import layout_table
    doc = _table_doc(norm)
    end_y = layout_table(ctx, doc.tables[0], y, x, w, default_pt)
    return end_y - y
