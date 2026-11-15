"""kenjoy_doc.py — 长文档抄写备料：txt 归一化+分页估算；docx 原样暂存。

用法：
  python kenjoy_doc.py --file notes.txt
  python kenjoy_doc.py --file paper.docx
  python kenjoy_doc.py --file notes.txt --font blood8-2_kvenjoy_blood8.gfont --lines 30
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kenjoy_common as kc  # noqa: E402
from kenjoy_prepare import apply_preset, normalize_text  # noqa: E402


def estimate_pages(text: str, lines_per_page: int, chars_per_line: int) -> int:
    """按行估算页数：每行折算 chars_per_line 个字符。"""
    total = 0
    for ln in text.split("\n"):
        total += max(1, math.ceil(len(ln) / max(1, chars_per_line)))
    return max(1, math.ceil(total / max(1, lines_per_page)))


def main() -> int:
    ap = argparse.ArgumentParser(description="长文档抄写备料")
    ap.add_argument("--file", required=True)
    ap.add_argument("--half-width", action="store_true")
    ap.add_argument("--no-clipboard", action="store_true")
    ap.add_argument("--preset", action="store_true")
    ap.add_argument("--font")
    ap.add_argument("--lines", type=int)
    ap.add_argument("--page-w", type=float, dest="page_w")
    ap.add_argument("--page-h", type=float, dest="page_h")
    ap.add_argument("--preset-name", default="默认")
    ap.add_argument("--chars-per-line", type=int, default=0,
                    help="分页估算用每行字数；0=按页面宽/字号估算")
    args = ap.parse_args()

    cfg = kc.load_config()
    src = Path(args.file)
    if not src.exists():
        print(f"文件不存在：{src}", file=sys.stderr)
        return 1

    outbox = kc.new_outbox(cfg)
    suffix = src.suffix.lower()

    if suffix == ".docx":
        dst = outbox / src.name
        shutil.copy2(src, dst)
        print(f"已暂存 docx：{dst}")
        kc.ready_markdown(outbox, [
            "# READY — 文档抄写（docx）",
            "",
            f"- 文件：`{dst}`",
            f"- 字体：{args.font or cfg.get('default_font') or '(GUI 中选择)'}",
            "",
            "## GUI 剩余操作（已实测核实）",
            "1. 打开 奎享雕刻.exe",
            "2. 左上模块切换器 → 「文字」模块，纸张选「A4普通边距」",
            f"3. 导入 `{src.name}`（文件→打开）⚠ 如无 docx 导入则在 Word 复制后粘贴到编辑区",
            "4. 确认字体后点「更新」→ 右栏「书写」开始",
        ])
        print(f"清单：{outbox / 'READY.md'}")
        return 0

    if suffix not in (".txt", ".md", ".text"):
        print(f"提示：{suffix} 未识别，按纯文本处理", file=sys.stderr)

    text = normalize_text(src.read_text(encoding="utf-8"), half_width=args.half_width)
    content = outbox / "content.txt"
    content.write_text(text, encoding="utf-8")

    lines_per_page = args.lines or int(cfg.get("default_page", {}).get("lineNum", 30))
    if args.chars_per_line > 0:
        cpl = args.chars_per_line
    else:
        page_w = args.page_w or cfg.get("default_page", {}).get("pageWidth", 210.0)
        font_size = 9.5  # mm，与 fontSize 95 对应的经验字高
        cpl = max(1, int((page_w - 63.6) / font_size))  # 减去默认左右边距 31.8*2
    pages = estimate_pages(text, lines_per_page, cpl)
    n_chars = len(text.replace("\n", ""))
    print(f"已写入 {content}（{n_chars} 字，约 {pages} 页，按 {lines_per_page} 行/页、{cpl} 字/行估算）")

    if not args.no_clipboard:
        from kenjoy_prepare import set_clipboard
        ok = set_clipboard(text)
        print("已复制到剪贴板" if ok else "剪贴板复制失败，请手动复制 content.txt")

    if args.preset:
        apply_preset(
            cfg,
            font=args.font or cfg.get("default_font") or None,
            lines=args.lines,
            page_w=args.page_w,
            page_h=args.page_h,
            name=args.preset_name,
        )

    kc.ready_markdown(outbox, [
        "# READY — 长文档抄写（txt）",
        "",
        f"- 内容文件：`{content}`",
        f"- 字数：{n_chars}，预估页数：{pages}",
        f"- 字体：{args.font or cfg.get('default_font') or '(GUI 中选择)'}",
        "",
        "## GUI 剩余操作（已实测核实）",
        "1. 打开 奎享雕刻.exe",
        "2. 左上模块切换器 → 「笔记」（抄写）或「文字」（富文本）",
        "3. 点「编辑内容」→ 粘贴剪贴板内容（或 插入→文字）",
        "4. 确认页面行数与分页后点「更新」",
        "5. 点右栏「书写」开始",
    ])
    print(f"清单：{outbox / 'READY.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
