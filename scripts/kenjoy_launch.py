"""kenjoy_launch.py — 应用暂存设置、启动 奎享雕刻.exe、输出 GUI 操作清单。

用法：
  python kenjoy_launch.py --feature text
  python kenjoy_launch.py --feature doc
  python kenjoy_launch.py --feature image
  python kenjoy_launch.py --feature vector
  python kenjoy_launch.py --feature text --outbox <备料目录>   # 指定要交接的 outbox
  python kenjoy_launch.py --feature text --no-start           # 只打印清单不启动
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kenjoy_common as kc  # noqa: E402

FEATURE_STEPS = {
    "text": [
        "左上模块切换器（汉堡图标）→ 选「笔记」（抄写）或「文字」（富文本）",
        "点「编辑内容」→ 粘贴剪贴板内容（文字模块用 插入→文字）",
        "点「字体」→ 字体对话框选字库（可多选加权）→ 确定选择",
        "调 页面/行数/字号 后点「更新」刷新预览",
        "点右栏「书写」开始（暂停/终止可用）",
    ],
    "doc": [
        "txt：模块「笔记」→ 编辑内容 粘贴 content.txt（分页按 行数/页面高 自动）",
        "docx：模块「文字」→ 纸张(A4普通边距) → 用 文件→打开 或编辑区导入 ⚠ 如无导入入口则在 Word 复制后粘贴",
        "确认字体与分页后点「更新」",
        "点右栏「书写」开始",
    ],
    "image": [
        "左上模块切换器 → 「实心图」（细化描摹）或「边缘图」（描边）",
        "载入 prepared_*.png（trace_*.svg 可走 SVG矢量图 模块）",
        "调 平滑/阈值（对应 thinning.json / edge.json）→ 点「更新」",
        "点右栏「书写」开始",
    ],
    "vector": [
        "SVG：模块「SVG矢量图」；DXF：模块「CAD图纸」",
        "载入 *_scaled.svg / *.dxf",
        "核对预览与图幅 → 点「更新」",
        "点右栏「书写」开始",
    ],
}


def latest_outbox(cfg: dict) -> Path | None:
    root = kc.outbox_root(cfg)
    if not root.is_dir():
        return None
    dirs = [p for p in root.iterdir() if p.is_dir() and p.name != "backups"]
    return max(dirs, key=lambda p: p.name) if dirs else None


def main() -> int:
    ap = argparse.ArgumentParser(description="启动奎享雕刻并交接")
    ap.add_argument("--feature", required=True, choices=sorted(FEATURE_STEPS))
    ap.add_argument("--outbox")
    ap.add_argument("--no-start", action="store_true")
    args = ap.parse_args()

    cfg = kc.load_config()
    inst = kc.find_install_dir(cfg)
    if inst is None:
        print("错误：找不到安装目录（奎享雕刻.exe）。先运行 kenjoy_config.py --init。", file=sys.stderr)
        return 1

    outbox = Path(args.outbox) if args.outbox else latest_outbox(cfg)
    if outbox and not outbox.is_dir():
        print(f"错误：outbox 不存在：{outbox}", file=sys.stderr)
        return 1

    # 串口提示（只读枚举）
    ports = kc.com_ports()
    try:
        cur_port = kc.read_json(kc.profile_dir(cfg) / "MachineControllerData.json").get("port", "")
    except Exception:
        cur_port = ""
    if ports and cur_port and cur_port not in ports:
        print(f"注意：配置的串口 {cur_port} 不在当前枚举中（{', '.join(ports)}）。")
        print("若机器重插过 USB，需在 GUI 里改端口，或在软件关闭后用 kenjoy_config.py --write 更新。")

    if not args.no_start:
        if kc.exe_running():
            print(f"{kc.EXE_NAME} 已在运行，跳过启动。")
        else:
            exe = inst / kc.EXE_NAME
            subprocess.Popen([str(exe)], cwd=str(inst))
            print(f"已启动：{exe}")

    print()
    print(f"== GUI 操作清单（功能：{args.feature}）==")
    for i, step in enumerate(FEATURE_STEPS[args.feature], 1):
        print(f"{i}. {step}")
    if outbox:
        ready = outbox / "READY.md"
        print()
        print(f"备料目录：{outbox}")
        if ready.exists():
            print(f"详见：{ready}")
    else:
        print("（未找到备料 outbox，可先运行 kenjoy_prepare / kenjoy_doc / kenjoy_image / kenjoy_vector）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
