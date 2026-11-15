"""gfont 字库读取 —— 奎享雕刻 .gfont 格式（实测）。

结构：[8字节头][字形数据段][ZIP]
ZIP 条目名 = Unicode 码点十进制字符串，值为：
    [u16 BE 码点][u16 0][u16 BE 浮点数个数][float32 BE x,y,...][u32 BE flags数][flags 字节]
flags 每点一字节：0x00 = 笔画起点（抬笔移过去再落笔），0x01 = 连笔。
坐标系：x 向右，y 向下，基线 y=0，字身约 y∈[-450,+21]（em≈470 单位）。
"""

from __future__ import annotations

import io
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

EM_UNITS = 470.0  # 字身高度（单位/字身），经验值
BASELINE = 0.0


@dataclass
class Glyph:
    char: str
    strokes: list[list[tuple[float, float]]] = field(default_factory=list)
    max_x: float = 0.0
    min_x: float = 0.0

    @property
    def advance(self) -> float:
        return self.max_x - self.min_x


class GFont:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        zpos = raw.find(b"PK\x03\x04")
        if zpos < 0:
            raise ValueError(f"不是 gfont（无 ZIP 段）: {self.path}")
        self.header = raw[:8]
        self._zf = zipfile.ZipFile(io.BytesIO(raw[zpos:]))
        self._names = set(self._zf.namelist())
        self._cache: dict[str, Glyph] = {}

    def chars(self) -> list[str]:
        out = []
        for n in self._zf.namelist():
            try:
                out.append(chr(int(n)))
            except ValueError:
                continue
        return out

    def __contains__(self, ch: str) -> bool:
        return str(ord(ch)) in self._names

    def glyph(self, ch: str) -> Glyph:
        if ch in self._cache:
            return self._cache[ch]
        key = str(ord(ch))
        if key not in self._names:
            raise KeyError(f"字库无此字: {ch!r}")
        g = self._decode(ch, self._zf.read(key))
        self._cache[ch] = g
        return g

    @staticmethod
    def _decode(ch: str, raw: bytes) -> Glyph:
        cp, zero, nfloat = struct.unpack(">HHH", raw[:6])
        floats = struct.unpack(f">{nfloat}f", raw[6 : 6 + nfloat * 4])
        off = 6 + nfloat * 4
        (nflags,) = struct.unpack(">I", raw[off : off + 4])
        flags = raw[off + 4 : off + 4 + nflags]
        points = [(floats[i], floats[i + 1]) for i in range(0, nfloat, 2)]
        strokes: list[list[tuple[float, float]]] = []
        cur: list[tuple[float, float]] = []
        for i, pt in enumerate(points):
            start = i < len(flags) and flags[i] == 0x00
            if start and cur:
                strokes.append(cur)
                cur = []
            cur.append(pt)
        if cur:
            strokes.append(cur)
        xs = [p[0] for s in strokes for p in s] or [0.0]
        return Glyph(char=ch, strokes=strokes, max_x=max(xs), min_x=min(xs))

    def close(self) -> None:
        self._zf.close()


