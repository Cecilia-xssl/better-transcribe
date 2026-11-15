"""纸张模板 —— 预印表单的定位填表。

模板 JSON：
{
  "name": "华南农业大学实验报告(正16K)",
  "paper_w": 185, "paper_h": 260,
  "slots": {
    "题目": {"x": 24, "y": 31.5, "size": 5.0, "max_w": 60},
    ...
  }
}
坐标：mm，原点=纸张左上角，y 为基线（向下为正）。
"""

from __future__ import annotations

import json
import math
import random
import re
from dataclasses import dataclass
from pathlib import Path

from layout import EM_UNITS, Stroke, _lookup, adv_units, expand_chars, normalize_char


@dataclass
class Slot:
    name: str
    x: float
    y: float          # 基线
    size: float = 5.0
    max_w: float = 60.0
    pages: str = "1"      # 写入页规则："1" / "1,3" / "2-4" / "all"（空=仅第 1 页）
    enabled: bool = True  # False=整条不写（栏位与值保留）
    mode: str = "text"    # 值模式："text"=字面写入 / "formula"=公式求值（formula.py）


class PaperTemplate:
    def __init__(self, path: str | Path):
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        self._init(d)

    @classmethod
    def from_dict(cls, d: dict) -> "PaperTemplate":
        p = cls.__new__(cls)
        p._init(d)
        return p

    def _init(self, d: dict) -> None:
        self.name = d.get("name", "template")
        self.paper_w = float(d["paper_w"])
        self.paper_h = float(d["paper_h"])
        self.origin_x = float(d.get("origin_x", 0.0))  # 预设原点
        self.origin_y = float(d.get("origin_y", 0.0))
        self.park_x = float(d.get("park_x", 180.0))    # 收尾停靠位置（机器坐标）
        self.park_y = float(d.get("park_y", -3.0))
        self.z_on = float(d.get("z_on", 10.0))         # 落笔 Z；抬笔行程 = z_on - z_off
        self.z_off = float(d.get("z_off", 0.0))
        self.tilt_x = float(d.get("tilt_x", 0.0))      # 立体补偿：右缘落笔修正 mm（+ 深）
        self.tilt_y = float(d.get("tilt_y", 0.0))      # 立体补偿：下缘落笔修正 mm（+ 深）
        self.feed = float(d.get("feed", 4800.0))       # 画线速度 mm/min
        self.travel = float(d.get("travel", 5000.0))   # 空程速度 mm/min
        self.body = d.get("body")        # {"x","y","w","h"} 表纸正文区
        self.next_page = d.get("next_page") or d.get("body")
        self.slots = {
            k: Slot(k, float(v["x"]), float(v["y"]),
                    float(v.get("size", 5.0)), float(v.get("max_w", 60.0)),
                    str(v.get("pages", "1") or "1"), bool(v.get("enabled", True)),
                    str(v.get("mode", "text") or "text"))
            for k, v in d["slots"].items()
        }


def text_width(ctx, text: str, size_mm: float) -> float:
    s = size_mm / EM_UNITS
    w = 0.0
    for ch in expand_chars(text):
        ch = normalize_char(ch)
        if ch == " ":
            w += size_mm * 0.35
            continue
        g = _lookup(ctx, ch)
        w += (adv_units(g, ch) * s) if g else size_mm * 0.9
    return w


def render_text(ctx, text: str, x: float, baseline: float,
                size_mm: float, jitter: float = 0.0,
                rng: random.Random | None = None) -> list[Stroke]:
    """在指定基线位置书写一行（宽度适配由调用方处理）。"""
    s = size_mm / EM_UNITS
    strokes: list[Stroke] = []
    cx = x
    for ch in expand_chars(text):
        ch = normalize_char(ch)
        if ch == " ":
            cx += size_mm * 0.35
            continue
        g = _lookup(ctx, ch)
        if g is None:
            cx += size_mm * 0.9
            continue
        off = -g.min_x * s
        if jitter > 0:
            if rng is None:
                rng = random.Random(7)
            j = jitter / 100.0
            dx, dy = rng.uniform(-j, j) * size_mm * 0.25, rng.uniform(-j, j) * size_mm * 0.25
            rot = rng.uniform(-j, j) * 0.05
        else:
            dx = dy = rot = 0.0
        cos_a, sin_a = math.cos(rot), math.sin(rot)
        for st in g.strokes:
            pts = []
            for px, py in st:
                ux = off + px * s
                uy = py * s
                pts.append((cx + ux * cos_a - uy * sin_a + dx,
                            baseline + ux * sin_a + uy * cos_a + dy))
            strokes.append(Stroke(pts))
        cx += adv_units(g, ch) * s
    return strokes


def pages_match(spec, page_no: int) -> bool:
    """写入页规则匹配：空=仅第 1 页；all/*=每页；支持 1,3 与 2-4 组合。"""
    s = str(spec if spec is not None else "").strip().lower()
    if not s:
        return page_no == 1
    if s in ("all", "*", "全部"):
        return True
    for part in re.split(r"[,，、;；\s]+", s):
        p = part.strip()
        if not p:
            continue
        m = re.match(r"^(\d+)\s*[-~—－]\s*(\d+)$", p)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if min(a, b) <= page_no <= max(a, b):
                return True
        elif p.isdigit() and int(p) == page_no:
            return True
    return False


def fill_sheet(tpl: PaperTemplate, ctx, fields: dict[str, str],
               jitter: float = 1.5, page_no: int = 1) -> list[Stroke]:
    """把 fields 写到模板槽位（超宽自动缩字号）。

    page_no=文件内页码（1 起）；只写 enabled 且写入页规则命中的栏位。
    """
    rng = random.Random(42)
    out: list[Stroke] = []
    for name, text in fields.items():
        slot = tpl.slots.get(name)
        if slot is None or not text:
            continue
        if not slot.enabled or not pages_match(slot.pages, page_no):
            continue
        w = text_width(ctx, text, slot.size)
        size = slot.size
        if w > slot.max_w and w > 0:
            size = slot.size * slot.max_w / w
        out.extend(render_text(ctx, text, slot.x, slot.y, size, jitter, rng))
    return out


def probe_marks(tpl: PaperTemplate, cross_mm: float = 2.0,
                edges: bool = True) -> list[Stroke]:
    """标定用十字标记（写在废纸上，叠放表单校对位置）。

    edges=True 时额外画边缘刻度：顶边 y=3 每 10mm 一个十字（至纸宽），
    左边 x=3 每 50mm 一个——用于读出纸的实际覆盖范围。
    """
    out: list[Stroke] = []
    h = cross_mm / 2

    def cross(cx: float, cy: float) -> None:
        out.append(Stroke([(cx - h, cy), (cx + h, cy)], dwell_ms=300.0))
        out.append(Stroke([(cx, cy - h), (cx, cy + h)], dwell_ms=300.0))

    for slot in tpl.slots.values():
        cross(slot.x, slot.y)
    if edges:
        x = 10.0
        while x <= tpl.paper_w + 0.1:
            cross(x, 3.0)
            x += 10.0
        y = 50.0
        while y <= tpl.paper_h - 10.0:
            cross(3.0, y)
            y += 50.0
    return out
