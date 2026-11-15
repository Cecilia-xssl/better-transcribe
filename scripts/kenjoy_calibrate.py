"""kenjoy_calibrate.py —— 校准定位：画参考标记核对纸张/栏位/正文区，持久化偏移补偿。

用法：
  python kenjoy_calibrate.py --marks --send     # 综合校准图：原点点+栏位十字+正文框+页边框
  python kenjoy_calibrate.py --dot --send       # 仅原点定位点（纸角轻点）
  python kenjoy_calibrate.py --frame --send     # 仅正文区框 + 页边框
  python kenjoy_calibrate.py --show             # 显示当前模板坐标与偏移补偿
  python kenjoy_calibrate.py --set-offset 15 -3 # 把补偿写入模板（持久生效）

标记含义（纸面坐标原点=纸左上角，已含模板 offset 补偿）：
  ·  原点点(0,0) —— 应落在纸右上角
  ×  四个栏位中心十字 —— 应压在印刷栏位起始处
  ▭  正文区框 —— 正文书写范围
  □  页边框 —— 纸张有效区
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
from layout import Stroke  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / "scripts" / "headless" / "templates"


def load_template(name: str) -> dict:
    p = Path(name)
    if not p.exists():
        p = TEMPLATES / name
    if not p.exists():
        p = TEMPLATES / (name + ".json")
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="校准定位")
    ap.add_argument("--template", default="scau-report-16k.json")
    ap.add_argument("--marks", action="store_true", help="综合校准图（点+十字+正文框）")
    ap.add_argument("--dot", action="store_true", help="仅原点定位点（纸右上角轻点）")
    ap.add_argument("--frame", action="store_true", help="仅正文框")
    ap.add_argument("--slot-test", nargs="+", type=float, metavar="N",
                    help="单栏位定位测试：X Y [最宽] —— 画十字+可写宽线")
    ap.add_argument("--show", action="store_true", help="显示模板坐标与补偿")
    ap.add_argument("--set-offset", nargs=2, type=float, metavar=("X", "Y"),
                    help="把偏移补偿写入模板（mm）")
    ap.add_argument("--offset-x", type=float, default=None, help="临时覆盖模板补偿 X")
    ap.add_argument("--offset-y", type=float, default=None, help="临时覆盖模板补偿 Y")
    ap.add_argument("--send", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--port", default="COM5")
    args = ap.parse_args()

    td = load_template(args.template)
    tpath = TEMPLATES / args.template
    if not tpath.exists():
        tpath = TEMPLATES / (args.template + ".json")

    # 显示
    if args.show or not (args.marks or args.dot or args.frame or args.slot_test or args.set_offset):
        print(f"模板：{td.get('name', args.template)}")
        print(f"纸张：{td['paper_w']} x {td['paper_h']} mm   偏移补偿：X{td.get('offset_x', 0):+g} Y{td.get('offset_y', 0):+g}")
        print(f"正文区：{td.get('body')}")
        for k, v in td.get("slots", {}).items():
            print(f"  栏位 {k}: x={v['x']} y={v['y']} 字高={v.get('size')} 最宽={v.get('max_w')}")
        if not (args.marks or args.dot or args.frame or args.slot_test or args.set_offset):
            return 0

    # 持久化补偿
    if args.set_offset:
        td["offset_x"] = float(args.set_offset[0])
        td["offset_y"] = float(args.set_offset[1])
        tpath.write_text(json.dumps(td, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入模板补偿：offset_x={td['offset_x']} offset_y={td['offset_y']} → {tpath}")
        return 0

    # 组装标记（校准类笔画统一驻留 300ms 出墨——轻点触纸太短会写不出/偏浅）
    marks: list[Stroke] = []
    if args.dot or args.marks:
        _dx, _dy = float(td.get("dot_x", 20.0)), float(td.get("dot_y", 0.0))
        marks.append(Stroke([(_dx, _dy), (_dx + 0.2, _dy)], dwell_ms=300.0))  # 定位轻点（模板 dot_x/dot_y）
    if args.slot_test:
        x, y = float(args.slot_test[0]), float(args.slot_test[1])
        mw = float(args.slot_test[2]) if len(args.slot_test) > 2 else 20.0
        h = 1.5
        marks += [Stroke([(x - h, y), (x + h, y)], dwell_ms=300.0),
                  Stroke([(x, y - h), (x, y + h)], dwell_ms=300.0)]
        marks.append(Stroke([(x, y), (x + mw, y)], dwell_ms=300.0))  # 可写宽示意线
    if args.marks:
        for s in td.get("slots", {}).values():
            x, y, h = float(s["x"]), float(s["y"]), 1.5
            marks.append(Stroke([(x - h, y), (x + h, y)], dwell_ms=300.0))
            marks.append(Stroke([(x, y - h), (x, y + h)], dwell_ms=300.0))
    if args.frame or args.marks:
        b = td.get("body") or {"x": -173, "y": 40, "w": 151, "h": 185}
        x0, y0 = float(b["x"]), float(b["y"])
        x1, y1 = x0 + float(b["w"]), y0 + float(b["h"])
        marks += [Stroke([(x0, y0), (x1, y0)], dwell_ms=300.0), Stroke([(x1, y0), (x1, y1)], dwell_ms=300.0),
                  Stroke([(x1, y1), (x0, y1)], dwell_ms=300.0), Stroke([(x0, y1), (x0, y0)], dwell_ms=300.0)]
        # （页边框已移除：避免与正文框混淆成"两圈"；纸边实物可见）

    off_x = args.offset_x if args.offset_x is not None else float(td.get("offset_x", 0.0))
    off_y = args.offset_y if args.offset_y is not None else float(td.get("offset_y", 0.0))
    pk_x = float(td.get("park_x", off_x))
    pk_y = float(td.get("park_y", off_y))
    gcfg = GCodeConfig(feed=float(td.get("feed", 4800.0)), travel=float(td.get("travel", 5000.0)),
                       z_on=float(td.get("z_on", 10.0)), z_off=float(td.get("z_off", 0.0)),
                       machine_w=float(td["paper_w"]), machine_h=float(td["paper_h"]),
                       origin=(off_x, off_y), park_x=pk_x, park_y=pk_y,
                       tilt_x=float(td.get("tilt_x", 0.0)), tilt_y=float(td.get("tilt_y", 0.0)),
                       dot_x=float(td.get("dot_x", 20.0)), dot_y=float(td.get("dot_y", 0.0)))
    lines = strokes_to_gcode(marks, gcfg)
    print(f"标记 {len(marks)} 笔（补偿 X{off_x:+g} Y{off_y:+g}，抬笔行程 {float(td.get('z_on',10))-float(td.get('z_off',0)):.1f}mm），G-code {len(lines)} 行")

    if args.send and not args.yes:
        print("真写需要 --yes 确认。", file=sys.stderr)
        return 1
    sys.path.insert(0, str(SKILL / "scripts" / "headless"))
    import grbl
    return grbl.stream(args.port, lines, dry_run=not args.send, prelude=["G21", "G90"],
                       park=(pk_x, pk_y))


if __name__ == "__main__":
    sys.exit(main())
