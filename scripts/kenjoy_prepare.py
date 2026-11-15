"""kenjoy_prepare.py — 文字抄写备料：归一化文本 → outbox/content.txt + 剪贴板 + 可选页面预设。

用法：
  python kenjoy_prepare.py --text "要写的内容"
  python kenjoy_prepare.py --file input.txt
  python kenjoy_prepare.py --text "..." --preset --font blood8-2_kvenjoy_blood8.gfont --lines 30
  python kenjoy_prepare.py --text "..." --no-clipboard
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kenjoy_common as kc  # noqa: E402

# 全角 → 半角（保留全角标点，中文书写常保留全角标点）
_FW_OFFSET = 0xFEE0


def normalize_text(s: str, half_width: bool = False) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # 去除除 \n \t 外的控制字符
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    if half_width:
        out = []
        for ch in s:
            code = ord(ch)
            if code == 0x3000:  # 全角空格
                out.append(" ")
            elif 0xFF01 <= code <= 0xFF5E:
                out.append(chr(code - _FW_OFFSET))
            else:
                out.append(ch)
        s = "".join(out)
    # 结尾去多余空行
    s = s.rstrip("\n") + "\n"
    return s


def set_clipboard(text: str) -> bool:
    import subprocess

    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $input"],
            input=text,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if p.returncode == 0:
            return True
    except Exception:
        pass
    try:
        p = subprocess.run(["clip"], input=text.encode("utf-16-le"), capture_output=True, timeout=20)
        return p.returncode == 0
    except Exception:
        return False


def apply_preset(cfg: dict, font: str | None, lines: int | None,
                 page_w: float | None, page_h: float | None, name: str = "默认") -> None:
    """键级更新 page_settings.json 中指定条目（软件须关闭）。"""
    fname = "page_settings.json"
    path = kc.profile_dir(cfg) / fname
    data = kc.read_json(path) if path.exists() else []
    if not isinstance(data, list):
        data = []
    entry = next((e for e in data if e.get("name") == name), None)
    if entry is None:
        entry = {"name": name}
        data.append(entry)
    if page_w:
        entry["pageWidth"] = float(page_w)
    if page_h:
        entry["pageHeight"] = float(page_h)
    if lines:
        entry["lineNum"] = int(lines)
    if font:
        entry["fontPath"] = [font]
        entry.setdefault("fontChance", [0])
    kc.write_setting(fname, data, cfg)
    print(f"页面预设「{name}」已更新：{entry.get('pageWidth')}x{entry.get('pageHeight')}mm "
          f"lines={entry.get('lineNum')} font={entry.get('fontPath')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="文字抄写备料")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--text")
    src.add_argument("--file")
    ap.add_argument("--half-width", action="store_true", help="全角字符转半角")
    ap.add_argument("--no-clipboard", action="store_true")
    ap.add_argument("--preset", action="store_true", help="同时写入页面预设（需软件关闭）")
    ap.add_argument("--font")
    ap.add_argument("--lines", type=int)
    ap.add_argument("--page-w", type=float, dest="page_w")
    ap.add_argument("--page-h", type=float, dest="page_h")
    ap.add_argument("--preset-name", default="默认")
    args = ap.parse_args()

    cfg = kc.load_config()
    raw = args.text if args.text is not None else Path(args.file).read_text(encoding="utf-8")
    text = normalize_text(raw, half_width=args.half_width)

    outbox = kc.new_outbox(cfg)
    content = outbox / "content.txt"
    content.write_text(text, encoding="utf-8")
    n_chars = len(text.replace("\n", ""))
    n_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    print(f"已写入 {content}（{n_chars} 字，{n_lines} 行）")

    if not args.no_clipboard:
        ok = set_clipboard(text)
        print("已复制到剪贴板（GUI 中直接粘贴）" if ok else "剪贴板复制失败，请手动打开 content.txt 复制")

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
        "# READY — 文字抄写",
        "",
        f"- 内容文件：`{content}`",
        f"- 字数：{n_chars}，行数：{n_lines}",
        f"- 字体：{args.font or cfg.get('default_font') or '(未设置，GUI 中选择)'}",
        f"- 页面预设：{'已写入「' + args.preset_name + '」' if args.preset else '未改（用 GUI 当前值）'}",
        "",
        "## GUI 剩余操作（已实测核实）",
        "1. 打开 奎享雕刻.exe（如未启动）",
        "2. 左上模块切换器（汉堡图标）→ 选「笔记」（抄写）或「文字」（富文本）",
        "3. 点「编辑内容」→ 粘贴剪贴板内容（文字模块用 插入→文字）",
        "4. 点「字体」选字库 → 确认页面后点「更新」",
        "5. 点右栏「书写」开始（暂停/终止可用）",
    ])
    print(f"清单：{outbox / 'READY.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
