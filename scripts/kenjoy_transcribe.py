"""kenjoy_transcribe.py —— 批量誊抄实验报告：每份 = 表头栏位 + 正文（跳过标题段）。

页面构成：第 1 页 = 表纸（表头 y≈17-30 + 正文区 y40-225），续页 = 换纸整页。
页间流程：停右下角 → 电机松开 → 换纸（不要移动笔架）→ 按回车继续。

用法（建议在独立控制台窗口运行）：
  python -u kenjoy_transcribe.py --report-dir "实验报告"
  python -u kenjoy_transcribe.py --report-dir ... --dry-run      # 只出预览和 gcode
  python -u kenjoy_transcribe.py --report-dir ... --skip "附件"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts" / "headless"))

import kenjoy_common as kc  # noqa: E402
from gcode import GCodeConfig, strokes_to_gcode, safe_lift_z  # noqa: E402
from gfont import GFont  # noqa: E402
from layout import Ctx, Stroke, layout_docx  # noqa: E402
from template import PaperTemplate, fill_sheet  # noqa: E402
import formula as fx  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / "scripts" / "headless" / "templates"


def render_pages(pages, width: float, height: float, out_png: Path, scale: int = 2) -> None:
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


def render_one_page(strokes, width: float, height: float, out_png: Path, scale: int = 4) -> None:
    from PIL import Image, ImageDraw

    W = int(width * scale)
    H = int(height * scale)
    im = Image.new("L", (W, H), 255)
    dr = ImageDraw.Draw(im)
    dr.rectangle([0, 0, W - 1, H - 1], outline=120)
    for s in strokes:
        pts = [(x * scale, y * scale) for x, y in s.points]
        if len(pts) >= 2:
            dr.line(pts, fill=0, width=2)
    im.save(out_png)


def locate_stroke(batch_pages, tx: float, ty: float):
    """按内容坐标最近邻定位 → (页索引, 笔索引, 匹配距离mm)。"""
    best = (1e18, 0, 0)
    for pi, pg in enumerate(batch_pages):
        for si, s in enumerate(pg):
            for (px, py) in s.points:
                d = (px - tx) ** 2 + (py - ty) ** 2
                if d < best[0]:
                    best = (d, pi, si)
    return best[1], best[2], best[0] ** 0.5


def enrich_stop_state(full_pages, full_labels, full_meta,
                      off_x: float, off_y: float, state_path=None) -> None:
    """断点机器坐标 → （第几份文件/文件内第几页/页内进度%），供网页自动回填起点。

    full_* 必须是未裁剪的完整页列表：进度%按整页绝对口径（下取整到 0.001%）。
    若传裁剪后的列表，手动起点后的分母只剩余量，回填值会整体偏小。
    """
    st_path = Path(state_path) if state_path else (
        Path.home() / ".KenjoyDraw" / "mimo-outbox" / "GRBL_STOP_STATE.json")
    if not st_path.exists() or not full_pages:
        return
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
        mpos = st.get("mpos")
        if not mpos:
            return
        pi, si, dist = locate_stroke(full_pages,
                                     float(mpos[0]) - off_x, float(mpos[1]) - off_y)
        ri, rp = full_meta[pi]
        pct_milli = si * 100000 // max(len(full_pages[pi]), 1)  # 下取整到 0.001%
        st["report_idx"] = int(ri)
        st["report_page"] = int(rp)
        st["progress_pct"] = pct_milli / 1000.0
        st["label"] = full_labels[pi] if pi < len(full_labels) else ""
        st_path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"断点已记录起点：{st['label']}（页内 {st['progress_pct']:.3f}%，匹配 {dist:.2f}mm）")
    except Exception:
        pass


def main() -> int:
    cfg = kc.load_config()
    ap = argparse.ArgumentParser(description="批量誊抄实验报告")
    ap.add_argument("--report-dir", required=False,
                    help="报告目录（与 --file 二选一）")
    ap.add_argument("--file", action="append", default=None,
                    help="显式文件（可多次）：按给出顺序誊抄，不扫描目录、不受跳过过滤")
    ap.add_argument("--name", default=None)
    ap.add_argument("--class", dest="klass", default=None)
    ap.add_argument("--date", default=None, help="留空=不写日期")
    ap.add_argument("--skip", action="append", default=None,
                    help="文件名含此串的跳过（可多次）；缺省取配置文件 skip")
    ap.add_argument("--font")
    ap.add_argument("--jitter", type=float, default=3.0)
    ap.add_argument("--pages-per-report", type=int, default=6)
    ap.add_argument("--feed", type=float, default=None, help="画线速度 mm/min（默认取模板）")
    ap.add_argument("--z-on", type=float, dest="z_on", default=None, help="落笔 Z mm（默认取模板）")
    ap.add_argument("--z-off", type=float, dest="z_off", default=None, help="抬笔 Z mm（默认取模板）")
    ap.add_argument("--tilt-x", type=float, dest="tilt_x", default=None,
                    help="立体补偿：右缘落笔修正 mm ±1（默认取模板）")
    ap.add_argument("--tilt-y", type=float, dest="tilt_y", default=None,
                    help="立体补偿：下缘落笔修正 mm ±1（默认取模板）")
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--offset-x", type=float, default=None,
                    help="整体 X 偏移补偿 mm（默认取模板 offset_x；L 顶点偏右时用负值）")
    ap.add_argument("--offset-y", type=float, default=None,
                    help="整体 Y 偏移补偿 mm（默认取模板 offset_y；L 顶点偏下时用负值）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-report", type=int, default=0,
                    help="从第 N 份文件开始写（1 起）")
    ap.add_argument("--report-page", type=int, default=0,
                    help="该文件的第 M 页（1 起）")
    ap.add_argument("--progress", type=float, default=None,
                    help="从该页进度 P%% 处继续（0-100，按笔画比例）")
    ap.add_argument("--skip-strokes", type=int, default=0,
                    help="高级：页内直接跳过 N 笔（与 --progress 互斥，优先）")
    ap.add_argument("--resume", action="store_true",
                    help="从上次暂停/中断位置继续（读 GRBL_STOP_STATE.json 自动定位断点）")
    ap.add_argument("--auto-confirm", action="store_true",
                    help="跳过启动前的人工确认（网页/自动化用）")
    ap.add_argument("--verify-dot", action=argparse.BooleanOptionalAction, default=None,
                    help="开始前画定位点并暂停校验（默认取模板 verify_dot）")
    ap.add_argument("--out")
    args = ap.parse_args()

    # 模板与字库
    tpath = TEMPLATES / "scau-report-16k.json"
    td = json.loads(tpath.read_text(encoding="utf-8"))
    err = fx.validate_slots(td)  # 公式先校验（防写到纸上才发现语法错）
    if err:
        print(err, file=sys.stderr)
        return 2
    tpl = PaperTemplate.from_dict(td)
    # 整体偏移补偿：命令行 > 模板 offset_x/offset_y（试写标定值，已固化）
    off_x = args.offset_x if args.offset_x is not None else float(td.get("offset_x", 0.0))
    off_y = args.offset_y if args.offset_y is not None else float(td.get("offset_y", 0.0))
    pk_x = float(td.get("park_x", off_x))   # 停靠位置（机器坐标，可自定义）
    pk_y = float(td.get("park_y", off_y))
    body_box = td["body"]
    boxes = [(float(body_box["x"]), float(body_box["y"]), float(body_box["w"]), float(body_box["h"]))] * args.pages_per_report

    font_name = args.font or cfg.get("default_font") or ""
    font_path = kc.find_font(font_name, cfg)
    if not font_path:
        tried = "；".join(str(d) for d in kc.font_search_dirs(cfg)) or "(无字库目录)"
        print(f"字体不可用：{font_name}（已查：{tried}）", file=sys.stderr)
        return 1
    font = GFont(font_path)
    ctx = Ctx(font, page_w=tpl.paper_w, page_h=tpl.paper_h,
              margin_l=0, margin_r=0, margin_t=0, margin_b=0)
    ctx.jitter_pct = args.jitter
    ctx.draw_charts = bool(td.get("draw_charts", True))  # 图表/CAD/SVG 描摹
    ctx.draw_images = bool(td.get("draw_images", True))  # 照片素描描摹

    # 报告清单：显式 --file（按给定顺序，不受跳过过滤）优先；否则扫描目录
    if args.file:
        reports = [Path(p) for p in args.file if Path(p).is_file()]
        if not reports:
            print("--file 指定的文件都不存在。", file=sys.stderr)
            return 1
        print(f"显式文件 {len(reports)} 份（按给定顺序）")
    else:
        if not args.report_dir:
            print("需要 --report-dir 或 --file。", file=sys.stderr)
            return 1
        reports = sorted(
            r for r in Path(args.report_dir).iterdir()
            if r.suffix.lower() in {".docx", ".doc", ".pdf", ".xlsx", ".xlsm", ".xmind"} and r.is_file()
        )
        skips = args.skip if args.skip is not None else [str(s) for s in (td.get("skip") or [])]
        skips = [s for s in skips if s]
        print(f"跳过关键词：{'、'.join(skips) if skips else '无'}"
              f"（{'命令行' if args.skip is not None else '配置文件'}）")
        reports = [r for r in reports if not any(s in r.stem for s in skips)]
    if not reports:
        print("没有可誊抄的报告。", file=sys.stderr)
        return 1

    outdir = Path(args.out) if args.out else kc.new_outbox(cfg)
    outdir.mkdir(parents=True, exist_ok=True)

    # 逐份排版：表头 + 正文（每份独立 ctx，避免缺失项/笔画累积串扰）
    def make_ctx() -> Ctx:
        c = Ctx(font, page_w=tpl.paper_w, page_h=tpl.paper_h,
                margin_l=0, margin_r=0, margin_t=0, margin_b=0)
        c.jitter_pct = args.jitter
        c.draw_charts = bool(td.get("draw_charts", True))
        c.draw_images = bool(td.get("draw_images", True))
        return c

    batch_pages = []
    page_labels = []  # 每页的人类标签（换纸提示）
    page_meta = []    # 每页 (报告序号, 报告内页号)，手动定位用
    report_page_lists = []  # 每份报告的完整页列表（未裁剪），用于逐页预览
    print(f"共 {len(reports)} 份报告，字库 {font_path.name}，抖动 {args.jitter}\n")
    for ri, rp0 in enumerate(reports, 1):
        try:
            rp = kc.ensure_docx(rp0)   # .doc/.pdf 自动转 .docx
        except Exception as e:
            print(f"转换失败 {rp0.name}：{e}", file=sys.stderr)
            return 1
        fctx = fx.make_ctx(stem=rp0.stem, name=rp0.name, idx=ri)

        def build_fields(page_no: int) -> dict:
            f = fx.resolve_slots(td, fctx, page_no)  # text=字面 / formula=公式按页求值
            if args.name is not None:
                f["姓名"] = args.name
            if args.klass is not None:
                f["专业班次"] = args.klass
            if args.date is not None:
                f["日期"] = args.date
            if not f.get("题目"):
                f["题目"] = rp0.stem  # 兼容：题目留空=文件名
            return f

        used_fields = {k: v for k, v in build_fields(1).items() if v}
        print(f"  栏位[{ri}] {used_fields}")

        if rp0.suffix.lower() == ".xmind":
            # 思维导图：树结构直绘进正文区（单页）
            import mindmap
            tree = mindmap.parse_xmind(rp0)
            ctx_r = make_ctx()
            bx, by, bw, bh = boxes[0]
            hd = fill_sheet(tpl, ctx_r, build_fields(1), jitter=args.jitter, page_no=1)
            mindmap.draw_tree(ctx_r, tree, bx, by, bw, bh)
            page = hd + list(ctx_r.strokes)
            report_page_lists.append([page])
            batch_pages.append(page)
            page_labels.append(f"{ri}/{len(reports)} {rp0.stem[:18]} 第1页")
            page_meta.append((ri, 1))
            print(f"  {ri}. {rp0.stem} → 1 页（思维导图），笔画 {len(page)}")
            continue

        def render(ctx_r: Ctx):
            # 每页各自的栏位笔画：写入页规则分发（pages/enabled）+ 公式逐页求值
            heads = {pn: fill_sheet(tpl, ctx_r, build_fields(pn), jitter=args.jitter, page_no=pn)
                     for pn in range(1, args.pages_per_report + 1)}
            r = layout_docx(rp, font, ctx_r, potrace_exe=None,
                            max_pages=args.pages_per_report, boxes=boxes,
                            skip_first_para=True)
            return heads, r

        ctx_r = make_ctx()
        heads, res = render(ctx_r)
        singles = [m for m in sorted(set(res.missing)) if len(m) == 1]
        if singles:
            from hw_fallback import HanziFallback
            fb = HanziFallback()
            fb.ensure(singles)  # 缺字拉取（缓存后离线）
            ctx_r = make_ctx()
            ctx_r.fallback = fb
            heads, res = render(ctx_r)

        pages = list(res.pages)
        if not pages:
            pages = [[]]
        for pi in range(len(pages)):
            pages[pi] = (heads.get(pi + 1) or []) + pages[pi]
        report_page_lists.append(list(pages))
        for pi, p in enumerate(pages):
            batch_pages.append(p)
            page_labels.append(f"{ri}/{len(reports)} {rp.stem[:18]} 第{pi + 1}页")
            page_meta.append((ri, pi + 1))
        extra = " ⚠内容超页已截断" if res.truncated else ""
        imgs = len([m for m in res.missing if m.startswith("[")])
        blanks = f"，图片留白 {imgs} 处" if imgs else ""
        print(f"  {ri}. {rp.stem} → {len(pages)} 页，笔画 {sum(len(p) for p in pages)}"
              f"{blanks}{extra}")

    # 逐页预览图 + manifest（在裁剪前，基于完整页列表）
    pages_dir = outdir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest_files = []
    for ri2, (rp0, rpages) in enumerate(zip(reports, report_page_lists), 1):
        for pi2, strokes in enumerate(rpages, 1):
            render_one_page(strokes, tpl.paper_w, tpl.paper_h,
                            pages_dir / f"f{ri2}p{pi2}.png")
        # 每页笔画进度→y 采样（0..100% 十等分）：预览高亮按真实笔画位置映射
        ymarks = []
        for strokes in rpages:
            n = len(strokes)
            marks = []
            for k in range(11):
                idx = min(n - 1, int(k * n / 10)) if n else 0
                if n:
                    ys = [p[1] for p in strokes[idx].points]
                    marks.append(round(sum(ys) / max(len(ys), 1), 2))
                else:
                    marks.append(0.0)
            ymarks.append(marks)
        manifest_files.append({"idx": ri2, "name": rp0.stem, "page_count": len(rpages),
                               "ymarks": ymarks})
    from datetime import datetime, timezone
    manifest = {"generated": datetime.now(timezone.utc).isoformat(), "files": manifest_files}
    (pages_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"逐页预览：{pages_dir}（{len(manifest_files)} 份）")

    total = len(batch_pages)
    print(f"\n合计 {total} 页。")

    # 未裁剪快照：断点回填的页内进度%必须按整页绝对口径（裁剪后再算只剩相对余量）
    full_pages = list(batch_pages)
    full_labels = list(page_labels)
    full_meta = list(page_meta)

    # 手动定位起点：第 N 份文件 / 文件内第 M 页 / 进度 P%
    if args.from_report or args.report_page or args.progress is not None:
        r_no = args.from_report or 1
        p_no = args.report_page or 1
        idx = [i for i, (rr, pp) in enumerate(page_meta) if rr == r_no and pp == p_no]
        if not idx:
            print(f"定位无效：第 {r_no} 份文件没有第 {p_no} 页（可用：{page_meta[:8]}…）", file=sys.stderr)
            return 1
        pi = idx[0]
        if args.skip_strokes:
            si = max(args.skip_strokes, 0)
        elif args.progress is not None:
            prog = min(max(args.progress, 0.0), 100.0)
            si = int(len(batch_pages[pi]) * prog / 100.0)
        else:
            si = 0
        print(f"手动起点：{page_labels[pi]}，进度 {args.progress if args.progress is not None else 0}%"
              f"（页内跳过 {si}/{len(batch_pages[pi])} 笔）")
        batch_pages = batch_pages[pi:]
        if si:
            batch_pages[0] = batch_pages[0][si:]
        page_labels = page_labels[pi:]
        page_meta = page_meta[pi:]
        total = len(batch_pages)

    # 断点续写：按保存的机器坐标定位最近笔画，跳过其前全部内容
    if args.resume:
        import json as _json
        st_path = Path.home() / ".KenjoyDraw" / "mimo-outbox" / "GRBL_STOP_STATE.json"
        if not st_path.exists():
            print("无断点状态文件，无法续写。", file=sys.stderr)
            return 1
        st = _json.loads(st_path.read_text(encoding="utf-8"))
        mpos = st.get("mpos")
        if not mpos:
            print("断点无位置信息，无法续写。", file=sys.stderr)
            return 1
        tx, ty = float(mpos[0]) - off_x, float(mpos[1]) - off_y  # 机器→内容坐标
        pi, si, dist = locate_stroke(batch_pages, tx, ty)
        print(f"断点定位：机器({mpos[0]:.1f},{mpos[1]:.1f}) → 第 {pi + 1} 页第 {si + 1} 笔"
              f"（匹配距离 {dist:.2f}mm，中断原因: {st.get('reason')}）")
        print(f">>> 请把【{page_labels[pi]}】这张纸按原位置放回机器")
        batch_pages = batch_pages[pi:]
        batch_pages[0] = batch_pages[0][si:]
        page_labels = page_labels[pi:]
        page_meta = page_meta[pi:]
        total = len(batch_pages)  # 裁剪后重算（否则 final 判断错、换纸标签越界）
    # 定位标记：原地轻点一下（0.2mm 戳点），不画线防震颤；驻留出墨（轻点太短会写不出）
    dot_x = float(td.get("dot_x", 20.0))
    dot_y = float(td.get("dot_y", 0.0))
    origin_mark = [Stroke([(dot_x, dot_y), (dot_x + 0.2, dot_y)], dwell_ms=300.0)]
    pass  # 定位校验闸默认关闭（模板 verify_dot 开启才画，避免起点跑去左上角干涉区）
    png = outdir / "preview.png"
    render_pages(batch_pages, tpl.paper_w, tpl.paper_h, png)
    print(f"预览：{png}")

    feed = args.feed if args.feed is not None else float(td.get("feed", 4800.0))
    z_on = args.z_on if args.z_on is not None else float(td.get("z_on", 10.0))
    z_off = args.z_off if args.z_off is not None else float(td.get("z_off", 0.0))
    travel = float(td.get("travel", 5000.0))
    tilt_x = args.tilt_x if args.tilt_x is not None else float(td.get("tilt_x", 0.0))
    tilt_y = args.tilt_y if args.tilt_y is not None else float(td.get("tilt_y", 0.0))
    tilt_x = max(-1.0, min(1.0, tilt_x))
    tilt_y = max(-1.0, min(1.0, tilt_y))
    print(f"抬笔行程：{z_on - z_off:.1f}mm（落笔Z {z_on} / 抬笔Z {z_off}） | 速度：画线 {feed:.0f} / 空程 {travel:.0f} mm/min")
    if tilt_x or tilt_y:
        print(f"立体补偿：右缘 {tilt_x:+.2f} / 下缘 {tilt_y:+.2f} mm（+ 加深，左上为基准）")
    gcfg = GCodeConfig(feed=feed, travel=travel, z_on=z_on, z_off=z_off,
                       machine_w=tpl.paper_w, machine_h=tpl.paper_h,
                       origin=(off_x, off_y), park_x=pk_x, park_y=pk_y,
                       tilt_x=tilt_x, tilt_y=tilt_y,
                       dot_x=dot_x, dot_y=dot_y)

    if off_x or off_y:
        print(f"偏移补偿：X{off_x:+.1f} Y{off_y:+.1f} mm（{'模板' if args.offset_x is None and args.offset_y is None else '命令行'}）")

    # 组装：定位点校验（可关）；页间换纸标记带报告标签
    all_lines: list[str] = []
    verify = args.verify_dot if args.verify_dot is not None else bool(td.get("verify_dot", False))
    if not args.resume and verify:
        # 画定位点并暂停：确认点落在纸角才写正文
        verify = [l for l in strokes_to_gcode(origin_mark, gcfg, final=False) if l != "; @swap-page"]
        verify.append("; @pause 定位校验：轻点应落在纸角。可微调纸张对准；对准→回车继续，偏了→输入 s 停止并报告偏差")
        all_lines.extend(verify)
    for i, page in enumerate(batch_pages):
        final = i == total - 1
        all_lines.extend(strokes_to_gcode(page, gcfg, final=final))
        if not final:
            all_lines.append(f"; @swap-page 换纸 → {page_labels[i + 1]}")

    gc = outdir / "out.gcode"
    gc.write_text("\n".join(all_lines), encoding="utf-8")
    print(f"G-code：{gc}（{len(all_lines)} 行，{total} 张纸）")

    if args.dry_run:
        print("dry-run：未发送。")
        return 0

    if args.auto_confirm:
        print("自动确认：直接开始（请确认纸张已按试写位置放好）")
    else:
        input("确认【第一张表纸按试写时的位置放好！】按回车开始誊抄…")

    import grbl
    # 定位沿用试写标定：坐标系=机器零点（不做推笔架定原点）
    rc = grbl.stream(args.port, all_lines, dry_run=False, park=(pk_x, pk_y),
                     prelude=["G21", "G90"], z_off=safe_lift_z(gcfg))
    print("誊抄结束，返回:", rc)
    if rc != 0:
        enrich_stop_state(full_pages, full_labels, full_meta, off_x, off_y)
    return rc


if __name__ == "__main__":
    import traceback

    _PORT = "COM5"
    _CTRL_HANDLER = None  # 必须保持引用，否则 ctypes 回调被 GC 后钩子失效

    def _release_motors():
        """任何退出路径都释放电机（$1=255 持久，不释放会锁死发热）。"""
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent / "headless"))
            import grbl as _g
            if _g.release_motors():
                print(">> 电机已释放（$1=25 + $SLP 断电）")
        except Exception:
            pass

    def _install_close_hook():
        """拦截窗口关闭/注销/关机事件，抢先松开电机。"""
        global _CTRL_HANDLER
        try:
            import ctypes
            from ctypes import wintypes
            HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def handler(ctrl):
                _release_motors()
                return False  # 继续默认的进程终止

            _CTRL_HANDLER = HandlerRoutine(handler)  # 模块级引用防 GC
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER, True)
        except Exception:
            pass

    class Tee:
        """输出同时进控制台和日志文件（窗口关闭后可查）。"""

        def __init__(self, f):
            self.f = f

        def write(self, s):
            try:
                self.f.write(s)
                self.f.flush()
            except Exception:
                pass
            sys.__stdout__.write(s)

        def flush(self):
            try:
                self.f.flush()
            except Exception:
                pass

    log_path = Path.home() / ".KenjoyDraw" / "mimo-outbox" / "transcribe.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "a", encoding="utf-8")
    logf.write(f"\n===== 启动 {__import__('datetime').datetime.now()} =====\n")
    sys.stdout = Tee(logf)
    sys.stderr = Tee(logf)
    _install_close_hook()
    try:
        rc = main()
    except Exception:
        traceback.print_exc()
        rc = 9
        print("发生异常，日志:", log_path)
        _release_motors()
    print("日志:", log_path)
    if rc != 0 and not any("auto" in a for a in sys.argv):
        try:
            input("程序异常结束（返回码 %s），窗口保持打开供查看，按回车关闭…" % rc)
        except Exception:
            pass
    sys.exit(rc)
