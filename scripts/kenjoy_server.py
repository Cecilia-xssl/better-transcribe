"""kenjoy_server.py —— 誊抄 Web 控制台（本地网页预览与操作）。

启动：python kenjoy_server.py   → 浏览器打开 http://127.0.0.1:8765

功能：任务参数、起点定位、文件纸堆预览、机器预设/特殊文本框/整体模板预览（可拖动）、
      校准定位、实时日志与断点状态。页面在 scripts/web/index.html。
仅监听 127.0.0.1（本机），无对外暴露。执行体仍是 kenjoy_transcribe.py。
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent  # 撰写系统/（本文件在 scripts/ 下）
SCRIPT = SKILL / "scripts" / "kenjoy_transcribe.py"
CALIBRATE = SKILL / "scripts" / "kenjoy_calibrate.py"
HEADLESS = SKILL / "scripts" / "headless"
HTML_PATH = SKILL / "scripts" / "web" / "index.html"
OUTBOX = Path.home() / ".KenjoyDraw" / "mimo-outbox"
LOG_FILE = OUTBOX / "transcribe.log"
STATE_FILE = OUTBOX / "GRBL_STOP_STATE.json"

HOST, PORT = "127.0.0.1", 8765
DEFAULT_DIR = "."

_job_lock = threading.Lock()
_job: dict = {"proc": None, "cmd": None, "started": None}

_PAGE_IMG = re.compile(r"^f\d+p\d+\.png$")


# ---------- 任务控制 ----------

def _spawn(args: list[str], allow_parallel: bool = False, script: Path | None = None) -> bool:
    creationflags = 0x00000010  # CREATE_NEW_CONSOLE
    script = script or SCRIPT
    with _job_lock:
        if allow_parallel:
            # 预览/校准（dry-run 或短任务）：独立进程，不占用任务记录
            subprocess.Popen([sys.executable, "-u", str(script)] + args,
                             cwd=str(SKILL), creationflags=creationflags)
            return True
        if _job["proc"] and _job["proc"].poll() is None:
            return False
        _job["proc"] = subprocess.Popen(
            [sys.executable, "-u", str(script)] + args,
            cwd=str(SKILL), creationflags=creationflags)
        _job["cmd"] = args
        _job["started"] = time.strftime("%H:%M:%S")
        return True


def _running() -> bool:
    with _job_lock:
        return bool(_job["proc"] and _job["proc"].poll() is None)


def _log_tail(n: int = 14) -> list[str]:
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _latest_preview() -> Path | None:
    try:
        cands = [p for p in OUTBOX.rglob("preview.png")]
        return max(cands, key=lambda p: p.stat().st_mtime) if cands else None
    except Exception:
        return None


def _latest_pages_dir() -> Path | None:
    """最近一次预览生成的 pages/ 目录（含 manifest.json 与 fNpM.png）。"""
    try:
        cands = list(OUTBOX.glob("*/pages/manifest.json"))
        if not cands:
            return None
        return max(cands, key=lambda p: p.stat().st_mtime).parent
    except Exception:
        return None


def _stop_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


TEMPLATE = SKILL / "scripts" / "headless" / "templates" / "scau-report-16k.json"


def _load_template() -> dict:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def _save_template(td: dict) -> None:
    import shutil
    bak_dir = OUTBOX / "backups"
    bak_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATE, bak_dir / f"template-{time.strftime('%Y%m%d-%H%M%S')}.json")
    TEMPLATE.write_text(json.dumps(td, ensure_ascii=False, indent=2), encoding="utf-8")


def _valid_template(td: dict) -> str | None:
    try:
        float(td["paper_w"]), float(td["paper_h"])
        b = td.get("body") or {}
        for k in ("x", "y", "w", "h"):
            if b:
                float(b[k])
        for name, s in (td.get("slots") or {}).items():
            float(s["x"]), float(s["y"]), float(s.get("size", 5)), float(s.get("max_w", 20))
        sk = td.get("skip")
        if sk is not None and not isinstance(sk, list):
            return "skip 须为字符串数组"
        for k in ("tilt_x", "tilt_y"):
            if td.get(k) is not None and abs(float(td[k])) > 1.0:
                return f"{k} 立体补偿须在 ±1mm 内"
    except Exception as e:
        return f"字段校验失败: {e}"
    try:
        sys.path.insert(0, str(HEADLESS))
        from formula import validate_slots
        err = validate_slots(td)
        if err:
            return err
    except Exception:
        pass
    return None


def _config_skips() -> list:
    try:
        return [str(s) for s in (_load_template().get("skip") or []) if str(s).strip()]
    except Exception:
        return []


def _task_args(body: dict) -> list[str]:
    q = _queue_load()
    if not q:
        # 队列空：从默认目录填充（跳过过滤取配置）后走同一通道（保持零参数旧流程）
        d = (body.get("dir") or DEFAULT_DIR).strip()
        if "skip" in body:
            sk = body.get("skip")
            parts = sk if isinstance(sk, list) else re.split(r"[,，、;；]+", str(sk or ""))
        else:
            parts = _config_skips()
        _queue_fill_dir(d, [str(p).strip() for p in parts if str(p).strip()])
        q = _queue_load()
    if q:
        args: list[str] = []
        for item in q:
            args += ["--file", str(item["path"])]
        return args
    d = (body.get("dir") or DEFAULT_DIR).strip()
    args = ["--report-dir", d]
    sk = body.get("skip")
    if sk is not None:
        parts = sk if isinstance(sk, list) else re.split(r"[,，、;；]+", str(sk or ""))
        for p in [str(x).strip() for x in parts if str(x).strip()]:
            args += ["--skip", p]
    return args
    if body.get("name"):
        args += ["--name", body["name"]]
    if body.get("klass"):
        args += ["--class", body["klass"]]
    if body.get("date"):
        args += ["--date", body["date"]]
    return args


QUEUE_FILE = OUTBOX / "queue.json"


def _queue_load() -> list:
    try:
        q = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return [x for x in q if isinstance(x, dict) and x.get("path")]
    except Exception:
        return []


def _queue_save(q: list) -> None:
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUEUE_FILE.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _queue_fill_dir(directory: str, skips: list) -> int:
    """目录下 doc/docx/pdf（跳过后）按名序追加入队列（幂等）。返回新增数。"""
    try:
        files = sorted(p for p in Path(directory).iterdir()
                       if p.suffix.lower() in (".docx", ".doc", ".pdf") and p.is_file())
    except Exception:
        return 0
    q = _queue_load()
    have = {x["path"] for x in q}
    n = 0
    for p in files:
        if any(s in p.stem for s in skips):
            continue
        sp = str(p)
        if sp in have:
            continue
        q.append({"path": sp, "name": p.stem})
        have.add(sp)
        n += 1
    if n:
        _queue_save(q)
    return n


def _locate_files(want: list, current: str) -> list:
    """逐个按 文件名+大小 在常用根下找唯一真实文件（浏览器取不到绝对路径）。"""
    names = {str(f.get("name") or "") for f in (want or []) if f.get("name")}
    cands: dict = {n: [] for n in names}
    roots = []
    if current:
        try:
            cur = Path(current)
            roots += [cur.parent, cur.parent.parent]
        except Exception:
            pass
    roots += [Path.home() / "Desktop",
              Path.home() / "Documents", Path.home() / "Downloads"]
    seen = set()
    for root in roots:
        try:
            root = root.resolve()
        except Exception:
            continue
        if not root.is_dir() or str(root) in seen:
            continue
        seen.add(str(root))
        stack = [(root, 0)]
        visited = 0
        while stack and visited < 40000:
            d, depth = stack.pop(0)
            visited += 1
            try:
                for child in d.iterdir():
                    try:
                        if child.is_file():
                            if child.name in cands:
                                try:
                                    cands[child.name].append((child, child.stat().st_size))
                                except OSError:
                                    pass
                        elif depth < 5:
                            stack.append((child, depth + 1))
                    except OSError:
                        continue
            except OSError:
                continue
    out = []
    for f in want or []:
        name = str(f.get("name") or "")
        size = int(f.get("size") or -1)
        hits = [p for p, s in cands.get(name, []) if size < 0 or s == size]
        out.append({"name": name, "path": str(hits[0]) if len(hits) == 1 else None})
    return out


def _locate_dir(folder: str, files: list, current: str):
    """按 文件夹名+文件名+大小 在常用根下找唯一真实目录（浏览器取不到绝对路径）。"""
    want = {str(f.get("name") or ""): int(f.get("size") or -1)
            for f in (files or []) if f.get("name")}
    if not folder or not want:
        return None
    roots = []
    if current:
        try:
            cur = Path(current)
            roots += [cur.parent, cur.parent.parent]
        except Exception:
            pass
    roots += [Path.home() / "Desktop",
              Path.home() / "Documents", Path.home() / "Downloads"]
    hits, seen = [], set()
    for root in roots:
        try:
            root = root.resolve()
        except Exception:
            continue
        if not root.is_dir() or str(root) in seen:
            continue
        seen.add(str(root))
        stack = [(root, 0)]
        visited = 0
        while stack and visited < 20000 and len(hits) < 6:
            d, depth = stack.pop(0)
            visited += 1
            try:
                for child in d.iterdir():
                    try:
                        if not child.is_dir():
                            continue
                    except OSError:
                        continue
                    if child.name == folder:
                        hits.append(child)
                    elif depth < 4:
                        stack.append((child, depth + 1))
            except OSError:
                continue
    good = []
    for h in hits:
        try:
            have = {p.name: p.stat().st_size for p in h.iterdir() if p.is_file()}
        except OSError:
            continue
        if all(n in have and (s < 0 or have[n] == s) for n, s in want.items()):
            good.append(h)
    return good[0] if len(good) == 1 else None


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass  # 客户端已断开（刷新/切页），正常现象

    def _bytes(self, data: bytes, ctype: str):
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            try:
                page = HTML_PATH.read_text(encoding="utf-8").replace("__DIR__", DEFAULT_DIR)
                self._bytes(page.encode("utf-8"), "text/html; charset=utf-8")
            except Exception as e:
                self._json({"ok": False, "err": f"页面加载失败: {e}"}, 500)
        elif self.path.startswith("/status"):
            p = _latest_preview()
            try:
                sys.path.insert(0, str(HEADLESS))
                import grbl as _g
                _port = _g.resolve_port("COM5")
            except Exception as _e:
                _port = str(_e)
            self._json({
                "port": _port,
                "running": _running(),
                "cmd": " ".join(_job.get("cmd") or []),
                "started": _job.get("started"),
                "log": _log_tail(),
                "state": _stop_state(),
                "preview": bool(p),
                "preview_ts": int(p.stat().st_mtime) if p else 0,
            })
        elif self.path.startswith("/api/stacks"):
            pd = _latest_pages_dir()
            files = []
            ts = 0
            if pd:
                try:
                    m = json.loads((pd / "manifest.json").read_text(encoding="utf-8"))
                    files = m.get("files") or []
                    ts = int((pd / "manifest.json").stat().st_mtime)
                except Exception:
                    files = []
            self._json({"files": files, "ts": ts})
        elif self.path.startswith("/api/queue"):
            self._json({"files": _queue_load()})
        elif self.path.startswith("/api/template"):
            self._json(_load_template())
        elif self.path.startswith("/page.png"):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            img = ""
            for part in q.split("&"):
                if part.startswith("img="):
                    img = part[4:]
            pd = _latest_pages_dir()
            if pd and _PAGE_IMG.match(img or ""):
                f = pd / img
                if f.exists():
                    self._bytes(f.read_bytes(), "image/png")
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path.startswith("/preview.png"):
            try:
                p = _latest_preview()
                if p and p.exists():
                    self._bytes(p.read_bytes(), "image/png")
                else:
                    self.send_response(404)
                    self.end_headers()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except Exception:
            body = {}
        op = self.path.strip("/").split("/")[-1]

        sys.path.insert(0, str(HEADLESS))
        import grbl

        if op in ("start", "resume", "preview"):
            args = _task_args(body)
            if op == "resume":
                args.append("--resume")
            if body.get("from_report"):
                args += ["--from-report", str(body["from_report"])]
            if body.get("report_page"):
                args += ["--report-page", str(body["report_page"])]
            if body.get("progress") is not None:
                args += ["--progress", str(body["progress"])]
            if body.get("skip_strokes"):
                args += ["--skip-strokes", str(body["skip_strokes"])]
            if op == "preview":
                args.append("--dry-run")
            else:
                args.append("--auto-confirm")
            ok = _spawn(args, allow_parallel=(op == "preview"))
            self._json({"ok": ok, "err": None if ok else "任务已在运行"})
        elif op == "calibrate":
            mode = body.get("mode", "marks")
            if mode not in ("marks", "dot", "frame"):
                self._json({"ok": False, "err": "bad mode"}, 400)
                return
            cargs = ["--" + mode, "--send", "--yes"]
            tpl = body.get("tpl")
            if tpl:
                err = _valid_template(tpl)
                if err:
                    self._json({"ok": False, "err": err})
                    return
                tmp = OUTBOX / "template-live.json"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(tpl, ensure_ascii=False), encoding="utf-8")
                cargs += ["--template", str(tmp)]
            ok = _spawn(cargs, allow_parallel=True, script=CALIBRATE)
            self._json({"ok": ok})
        elif op == "test-slot":
            x, y = body.get("x"), body.get("y")
            if x is None or y is None:
                self._json({"ok": False, "err": "缺少 x/y"}, 400)
                return
            sargs = ["--slot-test", str(x), str(y), str(body.get("max_w", 20)), "--send", "--yes"]
            ok = _spawn(sargs, allow_parallel=True, script=CALIBRATE)
            self._json({"ok": ok})
        elif op == "locate":
            # 浏览器选文件夹拿不到绝对路径：按 名称+文件名+大小 在常用根下定位真实目录
            hits = _locate_dir(str(body.get("folder") or ""),
                               body.get("files") or [],
                               str(body.get("path") or ""))
            if hits:
                self._json({"ok": True, "path": str(hits)})
            else:
                self._json({"ok": False, "err": "未定位到唯一目录", "need_import": True})
        elif op == "import-files":
            folder = re.sub(r"[^\w\u4e00-\u9fff.-]", "_", str(body.get("folder") or "imported"))[:80]
            fs = body.get("files") or []
            dest = OUTBOX / "import" / folder
            written: list = []
            try:
                if dest.exists():
                    import shutil
                    shutil.rmtree(dest)
                dest.mkdir(parents=True)
                n = 0
                for f in fs:
                    name = Path(str(f.get("name") or "")).name  # 防路径穿越
                    if not name.lower().endswith((".docx", ".doc", ".pdf", ".xlsx", ".xlsm", ".xmind")):
                        continue
                    (dest / name).write_bytes(base64.b64decode(f.get("data") or ""))
                    written.append(str(dest / name))
                    n += 1
            except Exception as e:
                self._json({"ok": False, "err": str(e)[:200]})
                return
            self._json({"ok": True, "path": str(dest), "count": n, "paths": written})
        elif op == "locate-files":
            self._json({"results": _locate_files(body.get("files") or [],
                                                 str(body.get("path") or ""))})
        elif op == "queue-add":
            q = _queue_load()
            have = {x["path"] for x in q}
            n = 0
            for p in (body.get("paths") or []):
                sp = str(p)
                if sp and sp not in have and Path(sp).is_file():
                    q.append({"path": sp, "name": Path(sp).stem})
                    have.add(sp)
                    n += 1
            _queue_save(q)
            self._json({"ok": True, "added": n, "count": len(q)})
        elif op == "queue-remove":
            try:
                i = int(body.get("index", -1))
            except (TypeError, ValueError):
                i = -1
            q = _queue_load()
            if 0 <= i < len(q):
                q.pop(i)
                _queue_save(q)
            self._json({"ok": True, "count": len(q)})
        elif op == "queue-clear":
            _queue_save([])
            self._json({"ok": True, "count": 0})
        elif op == "queue-reorder":
            order = [str(p) for p in (body.get("paths") or []) if p]
            q = _queue_load()
            by = {x["path"]: x for x in q}
            newq = [by.pop(p) for p in order if p in by]
            newq += [x for x in q if x["path"] in by]  # 未提及的保持原相对序排尾
            _queue_save(newq)
            self._json({"ok": True, "count": len(newq)})
        elif op == "detect-bg":
            # 背景图元素识别：检测横向下划线/框线 → 毫米坐标（生成栏位草稿用）
            try:
                data = str(body.get("image") or "")
                if "," in data:
                    data = data.split(",", 1)[1]
                raw = base64.b64decode(data)
                import io as _io
                from PIL import Image as _I
                import numpy as _np
                im = _I.open(_io.BytesIO(raw)).convert("L")
                pw = float(body.get("paper_w") or 190.0)
                ph = float(body.get("paper_h") or 263.0)
                if max(im.size) > 1600:
                    r = 1600.0 / max(im.size)
                    im = im.resize((max(1, int(im.size[0] * r)), max(1, int(im.size[1] * r))))
                a = _np.asarray(im, dtype=_np.uint8)
                H, W = a.shape
                # 大津阈值
                hist = _np.bincount(a.ravel(), minlength=256).astype(_np.float64)
                w0 = _np.cumsum(hist)
                w1 = w0[-1] - w0
                mids = _np.arange(256)
                s0 = _np.cumsum(hist * mids)
                with _np.errstate(divide="ignore", invalid="ignore"):
                    mu0 = s0 / w0
                    mu1 = (s0[-1] - s0) / w1
                    sb = w0 * w1 * (mu0 - mu1) ** 2
                thr = int(_np.nanargmax(sb))
                dark = a <= thr
                min_len = max(12, int(W * 0.04))
                segs = []
                for y in range(2, H - 2):
                    row = dark[y].astype(_np.int8)
                    d1 = _np.diff(_np.concatenate(([0], row, [0])))
                    starts = _np.flatnonzero(d1 == 1)
                    ends = _np.flatnonzero(d1 == -1)
                    for s, e in zip(starts, ends):
                        if e - s >= min_len:
                            up = int(dark[y - 2, s:e].sum())
                            dn = int(dark[y + 2, s:e].sum())
                            if up < (e - s) * 0.5 and dn < (e - s) * 0.5:
                                segs.append((y, int(s), int(e)))
                # 合并相邻行重叠段（线宽 2~5px 成线）
                lines = []
                for y, s, e in segs:
                    hit = None
                    for L in lines:
                        if abs(L["y"] - y) <= 4 and s < L["e"] and e > L["s"]:
                            hit = L
                            break
                    if hit:
                        hit["y"] = (hit["y"] * hit["n"] + y) / (hit["n"] + 1)
                        hit["n"] += 1
                        hit["s"], hit["e"] = min(hit["s"], s), max(hit["e"], e)
                    else:
                        lines.append({"y": float(y), "s": s, "e": e, "n": 1})
                out = []
                for L in sorted(lines, key=lambda L: (L["y"], L["s"])):
                    out.append({"x": round(L["s"] * pw / W, 2),
                                "y": round(min(L["y"] * ph / H, ph - 1), 2),
                                "w": round((L["e"] - L["s"]) * pw / W, 2)})
                self._json({"ok": True, "lines": out})
            except Exception as e:
                self._json({"ok": False, "err": str(e)[:200]})
        elif op == "save-machine":
            # 机器预设失焦确认后自动保存：只并入机器键，不覆盖表单里未保存的设计改动
            keys = ("offset_x", "offset_y", "tilt_x", "tilt_y", "park_x", "park_y",
                    "dot_x", "dot_y", "z_on", "z_off", "feed", "travel", "verify_dot")
            try:
                td = _load_template()
                for k in keys:
                    if k in body:
                        td[k] = body[k]
                err = _valid_template(td)
                if err:
                    self._json({"ok": False, "err": err})
                    return
                _save_template(td)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "err": str(e)[:200]})
        elif op == "save-offset":
            ox, oy = body.get("ox") or "0", body.get("oy") or "0"
            r = subprocess.run([sys.executable, "-u", str(CALIBRATE),
                                "--set-offset", str(ox), str(oy)],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30)
            self._json({"ok": r.returncode == 0, "err": r.stderr.strip()[:200] or None})
        elif op == "template":
            err = _valid_template(body)
            if err:
                self._json({"ok": False, "err": err})
            else:
                _save_template(body)
                self._json({"ok": True})
        elif op == "pause":
            grbl.request_pause()
            self._json({"ok": True})
        elif op == "stop":
            grbl.request_stop()
            self._json({"ok": True})
        else:
            self._json({"ok": False, "err": "unknown op"}, 404)


if __name__ == "__main__":
    import os

    print(f"写字机誊抄控制台：http://{HOST}:{PORT}   （Ctrl+C 或关闭窗口=退出服务）")
    if os.environ.get("KENJOY_OPEN_BROWSER") == "1":
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
