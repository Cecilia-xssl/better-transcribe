"""开源笔顺字库兜底 —— chanind/hanzi-writer-data (MIT)。

仅在显式调用时按需拉取缺字 JSON（api.github.com，纯数据），缓存到本地后离线复用。
坐标换算（与 kdraw-gfont-tools 一致，实测校准）：SVG(x,y, 1024) → gfont：
    gx = 0.4715*(x-512) + 1.8
    gy = -0.4587*(y-512) - 213.5
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from pathlib import Path

from gfont import Glyph

API = "https://api.github.com/repos/chanind/hanzi-writer-data/contents/data/{}.json"
CACHE_DIR = Path(__file__).resolve().parent / "hw_cache"


def fetch_char(ch: str, cache_dir: Path | None = None) -> dict:
    """拉取一个字的笔顺数据（有缓存则直接读）。返回 {"strokes": [...]}。"""
    cache_dir = cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{ch}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    url = API.format(urllib.parse.quote(ch))
    with urllib.request.urlopen(url, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8"))
    content = base64.b64decode(payload["content"]).decode("utf-8")
    data = json.loads(content)
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _to_gfont_strokes(medians: list[list[list[float]]]) -> list[list[tuple[float, float]]]:
    """medians = 每笔的中线点列（单笔路径，适合笔写）。"""
    out = []
    for seg in medians:
        pts = [(0.4715 * (p[0] - 512) + 1.8, -0.4587 * (p[1] - 512) - 213.5) for p in seg]
        if len(pts) >= 2:
            out.append(pts)
    return out


class HanziFallback:
    """兼容 GFont 接口的开源兜底字库（.glyph()）。"""

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or CACHE_DIR
        self._cache: dict[str, Glyph] = {}

    def __contains__(self, ch: str) -> bool:
        return (self.cache_dir / f"{ch}.json").exists()

    def ensure(self, chars: list[str]) -> list[str]:
        """批量拉取；返回仍缺失的字。"""
        missing = []
        for ch in chars:
            if not ch.strip():
                continue
            try:
                self.glyph(ch)
            except Exception:
                missing.append(ch)
        return missing

    def glyph(self, ch: str) -> Glyph:
        if ch in self._cache:
            return self._cache[ch]
        data = fetch_char(ch, self.cache_dir)
        strokes = _to_gfont_strokes(data.get("medians", []))
        xs = [p[0] for s in strokes for p in s] or [0.0]
        g = Glyph(char=ch, strokes=strokes, max_x=max(xs), min_x=min(xs))
        self._cache[ch] = g
        return g
