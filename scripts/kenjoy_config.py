"""kenjoy_config.py — 定位安装目录/机器配置、列字体、列 COM、备份/写设置。

用法：
  python kenjoy_config.py --init           # 生成/校正 config.json（交互或用 --install-dir 等参数）
  python kenjoy_config.py --check          # 检查安装目录、数据目录、字体目录
  python kenjoy_config.py --list-fonts     # 列出 gcodeFonts/*.gfont
  python kenjoy_config.py --com            # 枚举串口（只读，绝不打开）
  python kenjoy_config.py --backup         # 备份当前机器配置下所有 JSON
  python kenjoy_config.py --write FILE JSON_STR   # 键级合并写入（软件须关闭）
  python kenjoy_config.py --show [FILE]    # 查看设置文件
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kenjoy_common as kc  # noqa: E402


def cmd_init(args) -> int:
    cfg = kc.load_config()
    if args.install_dir:
        cfg["install_dir"] = args.install_dir
    if args.machine_profile:
        cfg["machine_profile"] = args.machine_profile
    if args.default_font:
        cfg["default_font"] = args.default_font
    if args.outbox_dir:
        cfg["outbox_dir"] = args.outbox_dir

    inst = kc.find_install_dir(cfg)
    if inst is None:
        print("警告：未找到安装目录（找不到 奎享雕刻.exe）。请用 --install-dir 指定。", file=sys.stderr)
    else:
        cfg["install_dir"] = str(inst)
        print(f"安装目录：{inst}")

    prof = kc.profile_dir(cfg)
    if not prof.is_dir():
        print(f"警告：机器配置目录不存在：{prof}", file=sys.stderr)
        names = [p.name for p in kc.kenjoy_data_dir().iterdir() if p.is_dir()] if kc.kenjoy_data_dir().is_dir() else []
        if names:
            print(f"可用机器名：{names}（用 --machine-profile 选择）", file=sys.stderr)
    else:
        print(f"机器配置：{prof}")

    fonts = kc.list_fonts()
    if not cfg.get("default_font") and fonts:
        cfg["default_font"] = fonts[0]
        print(f"默认字体（自动选第一个）：{fonts[0]}")
    if cfg.get("default_font") and fonts and cfg["default_font"] not in fonts:
        print(f"警告：默认字体不在 gcodeFonts 中：{cfg['default_font']}", file=sys.stderr)

    kc.save_config(cfg)
    print(f"已写入 {kc.CONFIG_PATH}")
    return 0


def cmd_check(args) -> int:
    cfg = kc.load_config()
    ok = True
    inst = kc.find_install_dir(cfg)
    if inst:
        print(f"[OK] 安装目录：{inst}")
    else:
        print(f"[缺失] 安装目录（试过：{cfg.get('install_dir')} 及默认路径）", file=sys.stderr)
        ok = False

    prof = kc.profile_dir(cfg)
    print(f"[{'OK' if prof.is_dir() else '缺失'}] 机器配置目录：{prof}")
    ok = ok and prof.is_dir()

    data = kc.kenjoy_data_dir()
    print(f"[{'OK' if data.is_dir() else '缺失'}] 数据根目录：{data}")

    fonts = kc.list_fonts()
    print(f"[{'OK' if fonts else '缺失'}] 字体：{len(fonts)} 个 .gfont")
    if kc.exe_running():
        print(f"[提示] {kc.EXE_NAME} 正在运行（写设置前需关闭）")
    ports = kc.com_ports()
    print(f"[提示] 串口：{', '.join(ports) if ports else '未检测到'}")
    return 0 if ok else 1


def cmd_list_fonts(args) -> int:
    cfg = kc.load_config()
    inst = kc.find_install_dir(cfg)
    fonts = kc.list_fonts()
    for i, name in enumerate(fonts, 1):
        mark = "  <- 默认" if name == cfg.get("default_font") else ""
        print(f"{i:3d}. {name}{mark}")
    if not fonts:
        print("未找到 gcodeFonts 目录或字体为空。", file=sys.stderr)
        return 1
    return 0


def cmd_com(args) -> int:
    ports = kc.com_ports()
    if not ports:
        print("未检测到串口。若机器已插 USB，请检查 CH340 驱动。")
        return 1
    cfg = kc.load_config()
    try:
        cur = kc.read_json(kc.profile_dir(cfg) / "MachineControllerData.json").get("port", "")
    except Exception:
        cur = ""
    for p in ports:
        mark = "  <- 当前配置" if p == cur else ""
        print(f"{p}{mark}")
    return 0


def cmd_backup(args) -> int:
    cfg = kc.load_config()
    prof = kc.profile_dir(cfg)
    if not prof.is_dir():
        print(f"配置目录不存在：{prof}", file=sys.stderr)
        return 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = kc.outbox_root(cfg) / "backups" / stamp
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in list(prof.glob("*.json")) + list(kc.kenjoy_data_dir().glob("*.json")):
        shutil.copy2(f, dst / f.name)
        n += 1
    print(f"已备份 {n} 个文件到 {dst}")
    return 0


def cmd_write(args) -> int:
    cfg = kc.load_config()
    p = kc.profile_dir(cfg) / args.file
    updates = json.loads(args.json)
    if not isinstance(updates, dict):
        print("JSON 必须是对象（键级合并）。", file=sys.stderr)
        return 1
    if p.exists():
        data = kc.read_json(p)
        if not isinstance(data, dict):
            print(f"{args.file} 不是对象结构，整文件替换需手工确认；拒绝写入。", file=sys.stderr)
            return 1
        data.update(updates)
    else:
        data = updates
    kc.write_setting(args.file, data, cfg)
    print(f"已写入 {p}")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_show(args) -> int:
    cfg = kc.load_config()
    if args.file:
        p = kc.profile_dir(cfg) / args.file
        if not p.exists():
            print(f"不存在：{p}", file=sys.stderr)
            return 1
        print(kc.read_json(p) if p.suffix == ".json" else p.read_text(encoding="utf-8", errors="replace"))
    else:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="kenjoy 配置工具")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--list-fonts", action="store_true")
    ap.add_argument("--com", action="store_true")
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--write", nargs=2, metavar=("FILE", "JSON_STR"))
    ap.add_argument("--show", nargs="?", const="", metavar="FILE")
    ap.add_argument("--install-dir")
    ap.add_argument("--machine-profile")
    ap.add_argument("--default-font")
    ap.add_argument("--outbox-dir")
    args = ap.parse_args()

    if args.init:
        return cmd_init(args)
    if args.check:
        return cmd_check(args)
    if args.list_fonts:
        return cmd_list_fonts(args)
    if args.com:
        return cmd_com(args)
    if args.backup:
        return cmd_backup(args)
    if args.write:
        return cmd_write(args)
    if args.show is not None:
        return cmd_show(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
