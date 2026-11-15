"""kenjoy_docx2draw.py —— Word 文档直出手写：docx → 排版 → 预览 + G-code（可选串口发送）。

用法：
  python kenjoy_docx2draw.py --file 文档.docx                     # 预览 + G-code（dry-run）
  python kenjoy_docx2draw.py --file 文档.docx --font 楷体_kvenjoy_大孔龙.gfont
  python kenjoy_docx2draw.py --file 文档.docx --send --port COM5  # 真正写（需 --yes 确认）
  python kenjoy_docx2draw.py --stop                               # 急停

版式范围：段落（对齐/缩进/字号/行距）、表格线框+单元格文字、图片（potrace 描摹）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts" / "headless"))

import kenjoy_common as kc  # noqa: E402
from gcode import GCodeConfig, safe_lift_z  # noqa: E402
from gfont import GFont  # noqa: E402
from layout import Ctx, layout_docx  # noqa: E402


def render_pages(pages, width: float, height: float, out_png: Path, scale: int = 3) -> None:
    """多页纵向堆叠预览。"""
    from PIL import Image, ImageDraw

    gap = 8
    W = int(width * scale)
    H = int((height + gap) * len(pages) * scale)
    im = Image.new("L", (W, H), 200)
    dr = ImageDraw.Draw(im)
    for si, strokes in enumerate(pages):
        oy = int((height + gap) * si * scale)
        dr.rectangle([0, oy, W - 1, oy + int(height * scale) - 1], fill=255, outline=120)
        for s in strokes:
            pts = [(x * scale, oy + y * scale) for x, y in s.points]
            if len(pts) >= 2:
                dr.line(pts, fill=0, width=2)
    im.save(out_png)


def main() -> int:
    cfg = kc.load_config()
    ap = argparse.ArgumentParser(description="docx 直出手写")
    ap.add_argument("--file")
    ap.add_argument("--font", help="gcodeFonts 下的文件名；默认取 config.default_font")
    ap.add_argument("--page-w", type=float, dest="page_w", default=210.0)
    ap.add_argument("--page-h", type=float, dest="page_h", default=297.0)
    ap.add_argument("--margin", type=float, default=25.4)
    ap.add_argument("--jitter", type=float, default=3.0, help="抖动 0-10（0=工整）")
    ap.add_argument("--fetch-missing", action="store_true",
                    help="缺字时从 hanzi-writer-data(MIT) 按需拉取兜底（需网络，拉取后缓存）")
    ap.add_argument("--pages", type=int, default=6, help="最多写几页（默认 6，超出截断）")
    ap.add_argument("--template", help="纸张模板（启用正文区/换纸页区域排版，如 scau-report-16k.json）")
    ap.add_argument("--skip-strokes", type=int, default=0,
                    help="跳过前 N 个笔画（断点续写用；每笔画以空程起笔，可安全跳过）")
    ap.add_argument("--skip-first-para", action="store_true",
                    help="跳过正文首段（标题已在表头题目栏写过时用）")
    ap.add_argument("--feed", type=float, default=4000.0)
    ap.add_argument("--z-on", type=float, dest="z_on", default=5.0)
    ap.add_argument("--z-off", type=float, dest="z_off", default=0.0)
    ap.add_argument("--send", action="store_true", help="串口发送（否则 dry-run）")
    ap.add_argument("--yes", action="store_true", help="确认真正动机器")
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--stop", action="store_true", help="请求急停")
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.stop:
        sys.path.insert(0, str(SKILL / "scripts" / "headless"))
        import grbl
        grbl.request_stop()
        print(f"已写停止文件：{grbl.STOP_FILE}")
        return 0

    if not args.file:
        ap.error("需要 --file")
    docx_path = Path(args.file)
    if not docx_path.exists():
        print(f"文件不存在：{docx_path}", file=sys.stderr)
        return 1
    try:
        docx_path = kc.ensure_docx(docx_path)  # .doc 自动转 .docx
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    inst = kc.find_install_dir(cfg)  # potrace 用（官方安装目录）
    font_name = args.font or cfg.get("default_font") or ""
    font_path = kc.find_font(font_name, cfg)
    if not font_path:
        fonts = kc.list_fonts()
        print(f"字体不可用：{font_name}。可用：{fonts[:10]}...", file=sys.stderr)
        return 1
    potrace = inst / "potrace-1.15.win32" / "potrace.exe" if inst else None

    outdir = Path(args.out) if args.out else kc.new_outbox(cfg)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"字库：{font_path.name}")
    font = GFont(font_path)

    # 模板模式：正文区 + 换纸页区域（同一标定坐标系）
    boxes = None
    page_w, page_h = args.page_w, args.page_h
    pk_x, pk_y = 180.0, -3.0  # 停靠位置（模板可改）
    td = {}
    if args.template:
        import json as _json
        tpath = Path(args.template)
        if not tpath.exists():
            tpath = SKILL / "scripts" / "headless" / "templates" / args.template
        if not tpath.exists():
            tpath = SKILL / "scripts" / "headless" / "templates" / (args.template + ".json")
        td = _json.loads(tpath.read_text(encoding="utf-8"))
        page_w, page_h = float(td["paper_w"]), float(td["paper_h"])
        body = td.get("body") or {"x": args.margin, "y": args.margin,
                                  "w": page_w - 2 * args.margin, "h": page_h - 2 * args.margin}
        boxes = [(float(body["x"]), float(body["y"]), float(body["w"]), float(body["h"]))]
        pk_x, pk_y = float(td.get("park_x", 180.0)), float(td.get("park_y", -3.0))
        boxes += [(float(body["x"]), float(body["y"]), float(body["w"]), float(body["h"]))] * (args.pages - 1)
        print(f"模板：{td.get('name', tpath.name)} 正文区 {body}")

    ctx = Ctx(font, page_w=page_w, page_h=page_h,
              margin_l=args.margin, margin_r=args.margin,
              margin_t=args.margin, margin_b=args.margin)
    ctx.jitter_pct = args.jitter
    ctx.draw_charts = bool(td.get("draw_charts", True))
    ctx.draw_images = bool(td.get("draw_images", True))
    res = layout_docx(docx_path, font, ctx, potrace_exe=potrace,
                      max_pages=args.pages, boxes=boxes,
                      skip_first_para=args.skip_first_para)

    if res.missing and args.fetch_missing:
        print(f"缺字 {len(set(res.missing))}，从 hanzi-writer-data 拉取兜底...")
        from hw_fallback import HanziFallback

        fb = HanziFallback()
        real_missing = [m for m in sorted(set(res.missing)) if len(m) == 1]
        still = fb.ensure(real_missing)
        ctx2 = Ctx(font, page_w=page_w, page_h=page_h,
                   margin_l=args.margin, margin_r=args.margin,
                   margin_t=args.margin, margin_b=args.margin)
        ctx2.jitter_pct = args.jitter
        ctx2.fallback = fb
        ctx2.draw_charts = bool(td.get("draw_charts", True))
        ctx2.draw_images = bool(td.get("draw_images", True))
        res = layout_docx(docx_path, font, ctx2, potrace_exe=potrace,
                          max_pages=args.pages, boxes=boxes,
                          skip_first_para=args.skip_first_para)
        if still:
            print(f"兜底后仍缺：{still}")

    print(f"笔画 {len(res.strokes)}，点数 {sum(len(s.points) for s in res.strokes)}，页数 {len(res.pages)}")

    if args.skip_strokes > 0:
        skip = args.skip_strokes
        new_pages = []
        for p in res.pages:
            if skip >= len(p):
                skip -= len(p)
                continue
            new_pages.append(p[skip:])
            skip = 0
        total_before = sum(len(p) for p in res.pages)
        res.pages = new_pages
        res.strokes = [st for p in new_pages for st in p]
        print(f"断点续写：跳过 {args.skip_strokes} 笔画，剩余 {len(res.strokes)}（原 {total_before}）")
    if res.truncated:
        print(f"注意：内容超出 {args.pages} 页已截断（用 --pages N 扩大）")
    if res.missing:
        print(f"缺字/告警 {len(res.missing)}：{''.join(m for m in res.missing if len(m) == 1) or res.missing}")

    png = outdir / "preview.png"
    render_pages(res.pages, res.width, res.height, png)
    print(f"预览：{png}（{len(res.pages)} 页）")

    from gcode import pages_to_gcode

    gcfg = GCodeConfig(feed=args.feed, z_on=args.z_on, z_off=args.z_off,
                       machine_w=page_w, machine_h=page_h,
                       park_x=pk_x, park_y=pk_y,
                       tilt_x=float(td.get("tilt_x", 0.0)), tilt_y=float(td.get("tilt_y", 0.0)),
                       dot_x=float(td.get("dot_x", 20.0)), dot_y=float(td.get("dot_y", 0.0)))
    lines = pages_to_gcode(res.pages, gcfg)
    gc = outdir / "out.gcode"
    gc.write_text("\n".join(lines), encoding="utf-8")
    print(f"G-code：{gc}（{len(lines)} 行）")

    sys.path.insert(0, str(SKILL / "scripts" / "headless"))
    import grbl

    if args.send and not args.yes:
        print("真写机器需要加 --yes 确认。", file=sys.stderr)
        return 1
    return grbl.stream(args.port, lines, dry_run=not args.send, z_off=safe_lift_z(gcfg))


if __name__ == "__main__":
    sys.exit(main())
