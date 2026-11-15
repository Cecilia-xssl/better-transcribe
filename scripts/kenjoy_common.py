"""kenjoy skill 公共工具：路径、配置加载、进程检查、原子写、备份。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

EXE_NAME = "奎享雕刻.exe"
SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.json"
DEFAULT_CONFIG = {
    "install_dir": "",
    "machine_profile": "默认Grbl机器",
    "default_font": "",
    "default_page": {"pageWidth": 210.0, "pageHeight": 297.0, "lineNum": 30},
    "outbox_dir": "%USERPROFILE%\\.KenjoyDraw\\mimo-outbox",
}

KNOWN_INSTALL_DIRS = [
    r"C:\Program Files (x86)\奎享雕刻",
]


def expand(path_str: str) -> Path:
    return Path(os.path.expandvars(path_str))


def load_config() -> dict:
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        cfg = dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def save_config(cfg: dict) -> None:
    atomic_write_json(CONFIG_PATH, cfg)


def kenjoy_data_dir() -> Path:
    return Path.home() / ".KenjoyDraw"


def profile_dir(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    return kenjoy_data_dir() / cfg["machine_profile"]


def outbox_root(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    return expand(cfg["outbox_dir"])


def new_outbox(cfg: dict | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = outbox_root(cfg) / stamp
    d = base
    n = 1
    while d.exists():
        n += 1
        d = Path(f"{base}-{n}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def exe_running() -> bool:
    """奎享雕刻.exe 是否在运行（tasklist 查询，中文进程名）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}"],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
            timeout=15,
        )
        return EXE_NAME in (out.stdout or "")
    except Exception:
        return False


def find_install_dir(cfg: dict | None = None) -> Path | None:
    cfg = cfg or load_config()
    candidates = [cfg.get("install_dir", "")] + KNOWN_INSTALL_DIRS
    for c in candidates:
        if c and (expand(c) / EXE_NAME).exists():
            return expand(c)
    return None


# 管线根 = 本仓库根（自适应部署位置）；外部字库目录可经 config.font_root 指定
PIPELINE_ROOT = SKILL_DIR


def font_search_dirs(cfg: dict | None = None) -> list[Path]:
    """字库目录候选（按优先序）：仓库自有 gcodeFonts → config.font_root → 官方安装目录。"""
    cfg = cfg or load_config()
    cands = [SKILL_DIR / "gcodeFonts"]
    fr = cfg.get("font_root") or ""
    if fr:
        cands.append(expand(fr))
    inst = find_install_dir(cfg)
    if inst:
        cands.append(inst / "gcodeFonts")
    out, seen = [], set()
    for c in cands:
        try:
            key = str(c.resolve())
            if key in seen:
                continue
            seen.add(key)
            if c.is_dir():
                out.append(c)
        except OSError:
            continue
    return out


def find_font(name: str, cfg: dict | None = None) -> Path | None:
    """按文件名找 .gfont（多根搜索）；找不到返回 None。"""
    if not name:
        return None
    for d in font_search_dirs(cfg):
        p = d / name
        if p.is_file():
            return p
    return None


def list_fonts(install_dir: Path | None = None) -> list[str]:
    dirs = [install_dir / "gcodeFonts"] if install_dir else font_search_dirs()
    names: set = set()
    for d in dirs:
        if d and d.is_dir():
            names.update(p.name for p in d.glob("*.gfont"))
    return sorted(names)


def com_ports() -> list[str]:
    """只读枚举串口名，绝不打开串口。"""
    ps = (
        "Add-Type -AssemblyName System.IO.Ports; "
        "[System.IO.Ports.SerialPort]::GetPortNames()"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=20,
        )
        ports = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
        return sorted(set(ports), key=lambda s: (len(s), s))
    except Exception:
        return []


def read_json(path: Path):
    """UTF-8 优先，失败回退 GBK。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError(f"无法解析 JSON: {path}")


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_file(path: Path, outbox: Path | None = None) -> Path | None:
    """把现有设置文件复制到 outbox/backups/<时间戳>/，返回备份目录。"""
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst_dir = (outbox or outbox_root()) / "backups" / stamp
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst_dir / path.name)
    return dst_dir


def require_app_closed() -> None:
    if exe_running():
        print(f"错误：{EXE_NAME} 正在运行。请先关闭软件再写入设置（避免被覆写）。", file=sys.stderr)
        sys.exit(2)


def write_setting(filename: str, data, cfg: dict | None = None) -> Path:
    """备份 + 原子写 profile 下的设置文件。要求软件已关闭。"""
    require_app_closed()
    cfg = cfg or load_config()
    p = profile_dir(cfg) / filename
    backup_file(p, outbox_root(cfg))
    atomic_write_json(p, data)
    return p


def ready_markdown(outbox: Path, lines: list[str]) -> Path:
    p = outbox / "READY.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _find_soffice() -> Path | None:
    """探测 LibreOffice 可执行（PATH + 常见安装目录）。"""
    import shutil as _sh
    for name in ("soffice", "libreoffice"):
        hit = _sh.which(name)
        if hit:
            return Path(hit)
    for cand in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ):
        if Path(cand).exists():
            return Path(cand)
    return None


def ensure_docx(path: str | Path, out_dir: Path | None = None) -> Path:
    """确保输入是 .docx：.doc / .pdf 自动转换，.docx 原样返回。

    转换产物缓存到 out_dir（默认 outbox/docx-cache/），文件未变则复用。
    """
    import subprocess
    import time

    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(src)

    suffix = src.suffix.lower()
    if suffix == ".pdf":
        out_dir = out_dir or (Path.home() / ".KenjoyDraw" / "mimo-outbox" / "docx-cache")
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / (src.stem + ".converted.docx")
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
        sys.path.insert(0, str(Path(__file__).resolve().parent / "headless"))
        from pdfconv import pdf_to_docx
        return pdf_to_docx(src, dst)

    if suffix in (".xlsx", ".xlsm"):
        out_dir = out_dir or (Path.home() / ".KenjoyDraw" / "mimo-outbox" / "docx-cache")
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / (src.stem + ".converted.docx")
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
        sys.path.insert(0, str(Path(__file__).resolve().parent / "headless"))
        from sheetconv import xlsx_to_docx
        return xlsx_to_docx(src, dst)

    if suffix != ".doc":
        return src

    out_dir = out_dir or (Path.home() / ".KenjoyDraw" / "mimo-outbox" / "docx-cache")
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / (src.stem + ".docx")
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst

    # 引擎 1：LibreOffice（无头转换）
    soffice = _find_soffice()
    if soffice:
        r = subprocess.run(
            [str(soffice), "--headless", "--convert-to", "docx", "--outdir", str(out_dir), str(src)],
            capture_output=True, timeout=180)
        if dst.exists():
            return dst
        raise RuntimeError(f"LibreOffice 转换失败：{r.stderr.decode(errors='replace')[:200]}")

    # 引擎 2：Word / WPS COM（已装 Office 或 WPS 时）
    try:
        import win32com.client as wc
        for prog in ("Word.Application", "KWPS.Application"):
            try:
                app = wc.Dispatch(prog)
            except Exception:
                continue
            try:
                app.Visible = False
                doc = app.Documents.Open(str(src.resolve()), ReadOnly=True)
                doc.SaveAs2(str(dst.resolve()), FileFormat=16)  # 16 = wdFormatXMLDocument (.docx)
                doc.Close(False)
                app.Quit()
                return dst
            finally:
                try:
                    app.Quit()
                except Exception:
                    pass
    except ImportError:
        pass

    raise RuntimeError(
        "转换 .doc 需要 LibreOffice 或 Word/WPS 之一。"
        "请安装 LibreOffice（winget install TheDocumentFoundation.LibreOffice）后重试；.docx 可直接用。")

