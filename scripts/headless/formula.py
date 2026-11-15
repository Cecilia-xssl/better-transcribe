"""栏位值公式 —— 安全的小型表达式引擎（mode="formula" 时求值，区别于字面文本）。

语法（类 Excel/代码，无 eval，纯手写解析）：
    expr := cmp
    cmp  := add [("=="|"!="|"<="|">="|"<"|">") add]
    add  := term (("+"|"-") term)*        # + 对字符串为拼接
    term := unary ("*" unary)*
    unary:= "-" unary | atom
    atom := NUMBER | STRING | NAME | NAME "(" [expr ("," expr)*] ")" | "(" expr ")"

变量（ctx）：stem 文件名(无扩展) / name 文件名(含扩展) / idx 第几份(1起)
            / page 文件内第几页(1起) / today YYYY-MM-DD
函数：str int upper lower pad(n,w) replace(s,a,b) slice(s,a,b) len if(c,a,b)
字符串用 ' 或 " 引用。空公式（全空白）→ 空字符串。
"""

from __future__ import annotations

import re
from datetime import date


class FormulaError(ValueError):
    """公式语法/求值错误（含表达式原文）。"""


_TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<num>\d+(?:\.\d+)?)
    | (?P<str>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
    | (?P<name>[A-Za-z_][A-Za-z_0-9]*)
    | (?P<op>==|!=|<=|>=|[-+*(),<>])
    )""", re.X)


def _tokenize(src: str) -> list[tuple[str, object]]:
    toks: list[tuple[str, object]] = []
    pos = 0
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m or m.end() == pos:
            raise FormulaError(f"无法识别的字符：{src[pos:pos+10]!r}（表达式：{src!r}）")
        pos = m.end()
        if m.group("num") is not None:
            s = m.group("num")
            toks.append(("num", float(s) if "." in s else int(s)))
        elif m.group("str") is not None:
            toks.append(("str", _unescape(m.group("str"))))
        elif m.group("name") is not None:
            toks.append(("name", m.group("name")))
        else:
            toks.append(("op", m.group("op")))
    toks.append(("end", None))
    return toks


def _unescape(lit: str) -> str:
    body = lit[1:-1]
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            out.append({"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}.get(n, n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = _tokenize(src)
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def take(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, val):
        t = self.take()
        if t[0] == "op" and t[1] == val:
            return t
        raise FormulaError(f"期望 {val!r}，得到 {t[1]!r}（表达式：{self.src!r}）")

    def parse(self):
        node = self.cmp()
        if self.peek()[0] != "end":
            raise FormulaError(f"多余的内容：{self.peek()[1]!r}（表达式：{self.src!r}）")
        return node

    def cmp(self):
        left = self.add()
        t = self.peek()
        if t[0] == "op" and t[1] in ("==", "!=", "<=", ">=", "<", ">"):
            self.take()
            return ("cmp", t[1], left, self.add())
        return left

    def add(self):
        node = self.term()
        while True:
            t = self.peek()
            if t[0] == "op" and t[1] in ("+", "-"):
                self.take()
                node = ("bin", t[1], node, self.term())
            else:
                return node

    def term(self):
        node = self.unary()
        while True:
            t = self.peek()
            if t[0] == "op" and t[1] == "*":
                self.take()
                node = ("bin", "*", node, self.unary())
            else:
                return node

    def unary(self):
        t = self.peek()
        if t[0] == "op" and t[1] == "-":
            self.take()
            return ("neg", self.unary())
        return self.atom()

    def atom(self):
        t = self.take()
        if t[0] == "num":
            return ("lit", t[1])
        if t[0] == "str":
            return ("lit", t[1])
        if t[0] == "name":
            name = t[1]
            nxt = self.peek()
            if nxt[0] == "op" and nxt[1] == "(":
                self.take()
                args = []
                if not (self.peek()[0] == "op" and self.peek()[1] == ")"):
                    args.append(self.cmp())
                    while self.peek()[0] == "op" and self.peek()[1] == ",":
                        self.take()
                        args.append(self.cmp())
                self.expect(")")
                return ("call", name, args)
            return ("var", name)
        if t[0] == "op" and t[1] == "(":
            node = self.cmp()
            self.expect(")")
            return node
        raise FormulaError(f"意外的记号 {t[1]!r}（表达式：{self.src!r}）")


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fn_str(x):
    if _is_num(x) and float(x) == int(x):
        return str(int(x))
    return str(x)


def _fn_int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        raise FormulaError(f"int() 无法转换：{x!r}")


def _fn_pad(n, w):
    return str(_fn_int(n)).zfill(max(0, _fn_int(w)))


def _fn_replace(s, a, b):
    return str(s).replace(str(a), str(b))


def _fn_slice(s, a, b=None):
    s = str(s)
    ai = _fn_int(a)
    return s[ai:] if b is None else s[ai:_fn_int(b)]


_FUNCS = {
    "str": (1, 1, lambda x: _fn_str(x)),
    "int": (1, 1, _fn_int),
    "upper": (1, 1, lambda s: str(s).upper()),
    "lower": (1, 1, lambda s: str(s).lower()),
    "pad": (2, 2, _fn_pad),
    "replace": (3, 3, _fn_replace),
    "slice": (2, 3, _fn_slice),
    "len": (1, 1, lambda s: len(str(s))),
    "if": (3, 3, lambda c, a, b: a if (str(c) not in ("", "0", "0.0") and c is not None and c != 0) else b),
}


def _eval(node, ctx: dict, src: str):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "var":
        name = node[1]
        if name not in ctx:
            raise FormulaError(f"未知变量 {name!r}（可用：{sorted(ctx)}；表达式：{src!r}）")
        return ctx[name]
    if kind == "call":
        name, args = node[1], node[2]
        if name not in _FUNCS:
            raise FormulaError(f"未知函数 {name!r}（可用：{sorted(_FUNCS)}；表达式：{src!r}）")
        lo, hi, fn = _FUNCS[name]
        if not (lo <= len(args) <= hi):
            raise FormulaError(f"{name}() 需要 {lo}~{hi} 个参数，给了 {len(args)}（表达式：{src!r}）")
        try:
            return fn(*[_eval(a, ctx, src) for a in args])
        except FormulaError:
            raise
        except Exception as e:
            raise FormulaError(f"{name}() 求值失败：{e}（表达式：{src!r}）")
    if kind == "neg":
        v = _eval(node[1], ctx, src)
        if not _is_num(v):
            raise FormulaError(f"一元负号只能用于数字，得到 {v!r}（表达式：{src!r}）")
        return -v
    if kind == "bin":
        op, l, r = node[1], _eval(node[2], ctx, src), _eval(node[3], ctx, src)
        if op == "+":
            if _is_num(l) and _is_num(r):
                return l + r
            return _fn_str(l) + _fn_str(r)
        if not (_is_num(l) and _is_num(r)):
            raise FormulaError(f"{op} 只能用于数字（{l!r} {op} {r!r}；表达式：{src!r}）")
        return l - r if op == "-" else l * r
    if kind == "cmp":
        op, l, r = node[1], _eval(node[2], ctx, src), _eval(node[3], ctx, src)
        if _is_num(l) and _is_num(r):
            a, b = l, r
        else:
            a, b = _fn_str(l), _fn_str(r)
        ok = {"==": a == b, "!=": a != b, "<": a < b, ">": a > b,
              "<=": a <= b, ">=": a >= b}[op]
        return 1 if ok else 0
    raise FormulaError(f"内部错误：未知节点 {kind!r}")


def evaluate(expr: str, ctx: dict) -> str:
    """求值公式，返回写入文本。空/全空白表达式 → ""。"""
    src = (expr or "").strip()
    if not src:
        return ""
    node = _Parser(src).parse()
    return _fn_str(_eval(node, ctx, src))


def make_ctx(stem: str, name: str, idx: int, page: int = 1,
             today: str | None = None) -> dict:
    return {"stem": stem, "name": name, "idx": idx, "page": page,
            "today": today if today is not None else date.today().isoformat()}


def resolve_slots(td: dict, base_ctx: dict, page_no: int) -> dict[str, str]:
    """按槽位 mode 解析值：text=字面，formula=公式求值（注入 page）。"""
    out: dict[str, str] = {}
    cx = dict(base_ctx)
    cx["page"] = page_no
    for name, s in (td.get("slots") or {}).items():
        v = s.get("value") or ""
        if (s.get("mode") or "text") == "formula":
            out[name] = evaluate(v, cx)
        else:
            out[name] = v
    return out


def validate_slots(td: dict) -> str | None:
    """启动前校验全部公式（模板保存/导入也用）。返回错误串或 None。"""
    probe = make_ctx(stem="示例文件", name="示例文件.docx", idx=1, page=1)
    for name, s in (td.get("slots") or {}).items():
        if (s.get("mode") or "text") != "formula":
            continue
        try:
            evaluate(s.get("value") or "", probe)
        except FormulaError as e:
            return f"栏位「{name}」公式错误: {e}"
    return None
