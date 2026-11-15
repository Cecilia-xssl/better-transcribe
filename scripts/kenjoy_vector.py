"""kenjoy_vector.py — SVG/DXF 绘图备料：SVG 校验并缩放到页面（mm），DXF 直传。

用法：
  python kenjoy_vector.py --file art.svg
  python kenjoy_vector.py --file art.dxf
  python kenjoy_vector.py --file art.svg --page-w 210 --page-h 297
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kenjoy_common as kc  # noqa: E402


def parse_len(v: str | None) -> float | None:
    """解析 SVG 长度值（px/mm/cm/in），统一返回 mm。无单位按 px（96dpi）。"""
    if not v:
        return None
    m = re.match(r"^\s*([0-9.]+)\s*(px|mm|cm|in|pt)?\s*$", v)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2) or "px"
    to_mm = {"px": 25.4 / 96, "mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72}
    return val * to_mm[unit]


def scale_svg(src: Path, dst: Path, target_w_mm: float, target_h_mm: float) -> tuple[float, float]:
    """按 viewBox/宽高等比缩放到目标框内（mm），返回实际图幅 mm。"""
    tree = ET.parse(src)
    root = tree.getroot()
    vb = root.get("viewBox")
    if vb:
        parts = [float(x) for x in re.split(r"[\s,]+", vb.strip())]
        vb_w, vb_h = parts[2], parts[3]
    else:
        vb_w = parse_len(root.get("width")) or 100.0
        vb_h = parse_len(root.get("height")) or 100.0
        # 无 viewBox 时按 px 解释宽高
        if not root.get("width"):
            pass
    r = min(target_w_mm / vb_w, target_h_mm / vb_h)
    new_w, new_h = vb_w * r, vb_h * r
    root.set("viewBox", f"0 0 {vb_w} {vb_h}")
    root.set("width", f"{new_w:.3f}mm")
    root.set("height", f"{new_h:.3f}mm")
    tree.write(dst, encoding="utf-8", xml_declaration=True)
    return new_w, new_h


def main() -> int:
    ap = argparse.ArgumentParser(description="SVG/DXF 绘图备料")
    ap.add_argument("--file", required=True)
    ap.add_argument("--page-w", type=float, dest="page_w")
    ap.add_argument("--page-h", type=float, dest="page_h")
    ap.add_argument("--margin", type=float, default=0.0, help="四边留白 mm")
    args = ap.parse_args()

    cfg = kc.load_config()
    src = Path(args.file)
    if not src.exists():
        print(f"文件不存在：{src}", file=sys.stderr)
        return 1

    page_w = args.page_w or cfg.get("default_page", {}).get("pageWidth", 210.0)
    page_h = args.page_h or cfg.get("default_page", {}).get("pageHeight", 297.0)
    box_w = page_w - 2 * args.margin
    box_h = page_h - 2 * args.margin

    outbox = kc.new_outbox(cfg)
    suffix = src.suffix.lower()

    if suffix == ".dxf":
        dst = outbox / src.name
        shutil.copy2(src, dst)
        print(f"已暂存 DXF：{dst}（目标页面 {page_w}x{page_h}mm，导入后在 GUI 核对缩放）")
        kc.ready_markdown(outbox, [
            "# READY — DXF 绘图",
            "",
            f"- 文件：`{dst}`",
            f"- 目标页面：{page_w}x{page_h}mm",
            "",
            "## GUI 剩余操作（已实测核实）",
            "1. 打开 奎享雕刻.exe",
            "2. 左上模块切换器 → 「CAD图纸」模块",
            f"3. 载入 `{src.name}`，核对缩放至 {box_w:.0f}x{box_h:.0f}mm 框内",
            "4. 点「更新」→ 右栏「书写」开始",
        ])
        print(f"清单：{outbox / 'READY.md'}")
        return 0

    if suffix != ".svg":
        print(f"错误：不支持的格式 {suffix}（仅 .svg/.dxf）。", file=sys.stderr)
        return 1

    # SVG 校验 + 缩放
    try:
        ET.parse(src)
    except ET.ParseError as e:
        print(f"SVG 解析失败：{e}", file=sys.stderr)
        return 1

    dst = outbox / f"{src.stem}_scaled.svg"
    w, h = scale_svg(src, dst, box_w, box_h)
    print(f"已写入 {dst}（{w:.1f}x{h:.1f}mm，页面 {page_w}x{page_h}mm）")

    kc.ready_markdown(outbox, [
        "# READY — SVG 绘图",
        "",
        f"- 文件：`{dst}`",
        f"- 图幅：{w:.1f}x{h:.1f}mm（页面 {page_w}x{page_h}mm）",
        "",
        "## GUI 剩余操作（已实测核实）",
        "1. 打开 奎享雕刻.exe",
        "2. 左上模块切换器 → 「SVG矢量图」模块",
        f"3. 载入 `{dst.name}`",
        "4. 核对预览后点「更新」→ 右栏「书写」开始",
    ])
    print(f"清单：{outbox / 'READY.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
