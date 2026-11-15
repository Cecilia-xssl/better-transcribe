"""kenjoy_fill.py —— 预印表单定位填表（批量，换纸分页）。

用法：
  # 标定：在废纸上画各填写位十字，叠放表单校对坐标
  python kenjoy_fill.py --template scau-report-16k.json --probe

  # 单份：fields.json = {"shared": {...}, "reports": [{"题目": "..."}]}
  python kenjoy_fill.py --template scau-report-16k.json --fields fields.json

  # 批量：题目取自 docx 文件名
  python kenjoy_fill.py --template scau-report-16k.json --batch-dir "实验报告目录" \
      --name 张三 --date 2026.11.12 --class "农学2班" --group "第3组"

  # 真写（默认 dry-run）
  python kenjoy_fill.py ... --send --yes --port COM5
  python kenjoy_fill.py --stop
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts" / "headless"))

import kenjoy_common as kc  # noqa: E402
from gcode import GCodeConfig, safe_lift_z  # noqa: E402
from gfont import GFont  # noqa: E402
from layout import Ctx  # noqa: E402
from template import PaperTemplate, fill_sheet, probe_marks  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / "scripts" / "headless" / "templates"


def render_preview(sheets: list[list], tpl: PaperTemplate, out_png: Path, scale: int = 4) -> None:
    """多张纸纵向堆叠预览（灰线=纸边）。"""
    from PIL import Image, ImageDraw

    gap = 6
    W = int(tpl.paper_w * scale)
    H = int((tpl.paper_h + gap) * len(sheets) * scale)
    im = Image.new("L", (W, H), 200)
    dr = ImageDraw.Draw(im)
    for si, strokes in enumerate(sheets):
        oy = int((tpl.paper_h + gap) * si * scale)
        dr.rectangle([0, oy, W - 1, oy + int(tpl.paper_h * scale) - 1], fill=255, outline=120)
        for s in strokes:
            pts = [(x * scale, oy + y * scale) for x, y in s.points]
            if len(pts) >= 2:
                dr.line(pts, fill=0, width=2)
    im.save(out_png)


def main() -> int:
    cfg = kc.load_config()
    ap = argparse.ArgumentParser(description="定位填表")
    ap.add_argument("--template", help="模板 JSON 文件名（templates/ 下）或完整路径")
    ap.add_argument("--fields", help="fields.json（shared + reports）")
    ap.add_argument("--batch-dir", help="报告 docx 目录，题目取文件名")
    ap.add_argument("--name", help="姓名（--batch-dir 用）")
    ap.add_argument("--date")
    ap.add_argument("--class", dest="klass", help="专业班次")
    ap.add_argument("--group", help="组别")
    ap.add_argument("--font")
    ap.add_argument("--jitter", type=float, default=1.5)
    ap.add_argument("--feed", type=float, default=3500.0)
    ap.add_argument("--flip-x", action=argparse.BooleanOptionalAction, default=False,
                    help="X 镜像（本机实测 1:1，默认关）")
    ap.add_argument("--flip-y", action=argparse.BooleanOptionalAction, default=False,
                    help="Y 翻转（本机实测 1:1，默认关）")
    ap.add_argument("--z-on", type=float, dest="z_on", default=5.0)
    ap.add_argument("--z-off", type=float, dest="z_off", default=0.0)
    ap.add_argument("--probe", action="store_true", help="只画标定十字（写废纸）")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.stop:
        import grbl
        grbl.request_stop()
        print(f"已写停止文件：{grbl.STOP_FILE}")
        return 0

    # 模板
    if not args.template:
        ap.error("需要 --template")
    tpath = Path(args.template)
    if not tpath.exists():
        tpath = TEMPLATES / args.template
    if not tpath.exists():
        tpath = TEMPLATES / (args.template + ".json")
    if not tpath.exists():
        print(f"模板不存在：{args.template}", file=sys.stderr)
        return 1
    tpl = PaperTemplate(tpath)
    print(f"模板：{tpl.name}（{tpl.paper_w}x{tpl.paper_h}mm）")

    # 字库
    font_name = args.font or cfg.get("default_font") or ""
    font_path = kc.find_font(font_name, cfg)
    if not font_path:
        print(f"字体不可用：{font_name}", file=sys.stderr)
        return 1
    font = GFont(font_path)
    ctx = Ctx(font, page_w=tpl.paper_w, page_h=tpl.paper_h,
              margin_l=0, margin_r=0, margin_t=0, margin_b=0)

    outdir = Path(args.out) if args.out else kc.new_outbox(cfg)
    outdir.mkdir(parents=True, exist_ok=True)

    # 标定模式
    if args.probe:
        strokes = probe_marks(tpl)
        _emit(outdir, [strokes], tpl, args, cfg, single_ok=True)
        return 0

    # 组装每份内容
    sheets_fields: list[dict[str, str]] = []
    if args.fields:
        d = json.loads(Path(args.fields).read_text(encoding="utf-8"))
        shared = d.get("shared", {})
        for rep in d.get("reports", []):
            f = dict(shared)
            f.update(rep)
            sheets_fields.append(f)
    elif args.batch_dir:
        shared = {}
        for k, v in [("姓名", args.name), ("日期", args.date),
                     ("专业班次", args.klass), ("组别", args.group)]:
            if v:
                shared[k] = v
        for f in sorted(Path(args.batch_dir).glob("*.docx")):
            sheets_fields.append({**shared, "题目": f.stem})
    else:
        ap.error("需要 --fields 或 --batch-dir")

    if not sheets_fields:
        print("没有要填的内容。", file=sys.stderr)
        return 1

    sheets = []
    missing_all: set[str] = set()
    for i, f in enumerate(sheets_fields):
        before = len(ctx.missing)
        st = fill_sheet(tpl, ctx, f, jitter=args.jitter)
        missing_all.update(ctx.missing[before:])
        sheets.append(st)
        print(f"第 {i + 1} 份：{f}")

    if missing_all:
        print(f"缺字：{sorted(missing_all)}")

    _emit(outdir, sheets, tpl, args, cfg, single_ok=len(sheets) == 1)
    return 0


def _emit(outdir, sheets, tpl, args, cfg, single_ok: bool) -> None:
    png = outdir / "preview.png"
    render_preview(sheets, tpl, png)
    print(f"预览：{png}")

    gcfg = GCodeConfig(feed=args.feed, z_on=args.z_on, z_off=args.z_off,
                       machine_w=tpl.paper_w, machine_h=tpl.paper_h,
                       flip_x=args.flip_x, flip_y=args.flip_y,
                       park_x=getattr(tpl, 'park_x', 180.0), park_y=getattr(tpl, 'park_y', -3.0),
                       tilt_x=getattr(tpl, 'tilt_x', 0.0), tilt_y=getattr(tpl, 'tilt_y', 0.0),
                       dot_x=getattr(tpl, 'dot_x', 20.0), dot_y=getattr(tpl, 'dot_y', 0.0))
    from gcode import pages_to_gcode
    all_lines = pages_to_gcode(sheets, gcfg)
    gc = outdir / "out.gcode"
    gc.write_text("\n".join(all_lines), encoding="utf-8")
    print(f"G-code：{gc}（{len(all_lines)} 行，{len(sheets)} 张纸）")

    import grbl

    if args.send and not args.yes:
        print("真写机器需要加 --yes 确认。", file=sys.stderr)
        sys.exit(1)
    sys.exit(grbl.stream(args.port, all_lines, dry_run=not args.send, z_off=safe_lift_z(gcfg)))


if __name__ == "__main__":
    sys.exit(main())
