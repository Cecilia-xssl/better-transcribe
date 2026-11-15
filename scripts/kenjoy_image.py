"""kenjoy_image.py — 图片描绘备料：缩放/对比度/二值化，可选 potrace 描摹为 SVG。

用法：
  python kenjoy_image.py --file photo.jpg
  python kenjoy_image.py --file photo.jpg --trace          # 额外出 trace.svg
  python kenjoy_image.py --file photo.jpg --threshold 128 --contrast 1.4
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kenjoy_common as kc  # noqa: E402

try:
    from PIL import Image, ImageEnhance, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def fit_size(w: int, h: int, max_side: int) -> tuple[int, int]:
    if max(w, h) <= max_side:
        return w, h
    r = max_side / max(w, h)
    return max(1, int(w * r)), max(1, int(h * r))


def main() -> int:
    ap = argparse.ArgumentParser(description="图片描绘备料")
    ap.add_argument("--file", required=True)
    ap.add_argument("--trace", action="store_true", help="调用产品 potrace 描摹出 SVG")
    ap.add_argument("--threshold", type=int, default=128, help="二值化阈值 0-255")
    ap.add_argument("--contrast", type=float, default=1.0)
    ap.add_argument("--max-image-size", type=int, default=0,
                    help="最长边像素上限；0=取 settings.json 的 maxImageSize 或 4000")
    ap.add_argument("--keep-gray", action="store_true", help="只灰度不二值化")
    args = ap.parse_args()

    if not HAS_PIL:
        print("错误：需要 Pillow（pip install Pillow）。", file=sys.stderr)
        return 1

    cfg = kc.load_config()
    src = Path(args.file)
    if not src.exists():
        print(f"文件不存在：{src}", file=sys.stderr)
        return 1

    max_side = args.max_image_size
    if not max_side:
        try:
            st = kc.read_json(kc.profile_dir(cfg) / "settings.json")
            max_side = int(st.get("maxImageSize", 4000))
        except Exception:
            max_side = 4000

    outbox = kc.new_outbox(cfg)
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    w, h = fit_size(img.width, img.height, max_side)
    if (w, h) != (img.width, img.height):
        img = img.resize((w, h), Image.LANCZOS)
        print(f"缩放至 {w}x{h}（上限 {max_side}）")

    if args.contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(args.contrast)

    gray = img.convert("L")
    out_img = gray if args.keep_gray else gray.point(lambda p: 255 if p >= args.threshold else 0, mode="1").convert("L")
    prepared = outbox / f"prepared_{src.stem}.png"
    out_img.save(prepared)
    print(f"已写入 {prepared}（{'灰度' if args.keep_gray else '二值化'}，阈值 {args.threshold}）")

    trace_path = None
    if args.trace:
        # potrace 支持 BMP/PGM/PPM/PBM 输入，这里统一给二值 BMP
        bmp = outbox / f"prepared_{src.stem}.bmp"
        out_img.save(bmp, format="BMP")

        install = kc.find_install_dir(cfg)
        potrace = install / "potrace-1.15.win32" / "potrace.exe" if install else None
        if not potrace or not potrace.exists():
            print(f"未找到 potrace：{potrace}", file=sys.stderr)
            return 1
        trace_path = outbox / f"trace_{src.stem}.svg"
        cmd = [str(potrace), "-b", "svg", "-o", str(trace_path), str(bmp)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not trace_path.exists():
            print(f"potrace 失败：{r.stderr}", file=sys.stderr)
            return 1
        print(f"描摹输出：{trace_path}")

    kc.ready_markdown(outbox, [
        "# READY — 图片描绘",
        "",
        f"- 预处理图：`{prepared}`" + (f"\n- 描摹 SVG：`{trace_path}`" if trace_path else ""),
        f"- 参数：阈值 {args.threshold}，对比度 {args.contrast}，最长边 {max_side}px",
        "",
        "## GUI 剩余操作（已实测核实）",
        "1. 打开 奎享雕刻.exe",
        "2. 左上模块切换器 → 「实心图」（细化描摹）或「边缘图」（描边）",
        f"3. 载入 `{prepared.name}`" + ("（trace.svg 走「SVG矢量图」模块）" if trace_path else ""),
        "4. 调 平滑/阈值 后点「更新」",
        "5. 点右栏「书写」开始",
    ])
    print(f"清单：{outbox / 'READY.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
