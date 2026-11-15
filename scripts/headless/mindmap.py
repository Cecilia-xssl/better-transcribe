"""mindmap.py —— 树/思维导图：.xmind / SmartArt(dgm:dataModel) 解析 + 手绘导图渲染。

节点树统一为 {"text": str, "children": [节点, ...]}。
渲染为经典右向导图：根节点居左垂直居中，子树向右分层展开，
连接线为二次贝塞尔折线，节点为圆角胶囊轮廓 + 写字字库文字，整体等比缩放装入给定框。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET

# 节点/字号（设计空间 mm，随后整体等比缩放入框）
_DESIGN_ROOT_MM = 4.0        # 设计空间根字号
_LEVEL_SHRINK = 0.88         # 每层字号衰减
_MIN_SIZE_MM = 1.6           # 成稿字号下限
_CAP_H_K = 1.9               # 胶囊高 = 字号 × 1.9
_CAP_PAD_K = 0.5             # 胶囊左右内边距 = 字号 × 0.5
_HGAP_K = 1.3                # 层间横向间隙 = 根字号 × 1.3
_VGAP_K = 0.6                # 叶行间纵向间隙 = 根字号 × 0.6
_MAX_SCALE = 3.5             # 等比放大上限（防单节点撑爆全框）
_BEZIER_SEG = 20             # 二次贝塞尔折线化段数
_ARC_SEG = 14                # 胶囊端圆弧段数
_ELLIPSIS = "..."

# gfont 字身（layout.EM_TOP / gfont.EM_UNITS 同源，避免模块级 import layout）
_EM_UNITS = 470.0
_EM_TOP = 450.0
_EM_BOTTOM = 21.0


def _local(tag) -> str:
    """XML 元素名去名字空间（局部名匹配）。"""
    if isinstance(tag, str) and "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag or ""


def _clean_title(s) -> str:
    """标题去 HTML 标签/实体、折叠空白为单个空格。"""
    if s is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(s))
    try:
        import html as _html
        s = _html.unescape(s)
    except Exception:
        pass
    return " ".join(s.split())


# ---------------------------------------------------------------- xmind 解析

def parse_xmind(path) -> dict:
    """.xmind（zip）→ {"text":..., "children":[...]} 根节点树。
    支持新版 content.json（topic 树：title/children）与旧版 content.xml（topic 元素 xhtml:title）。
    解析失败抛 RuntimeError（带原因）。"""
    try:
        if isinstance(path, (bytes, bytearray)):
            zf = zipfile.ZipFile(io.BytesIO(bytes(path)))
        else:
            zf = zipfile.ZipFile(str(path))
    except Exception as e:
        raise RuntimeError(f"xmind 打开失败（不是有效 zip/xmind）：{e}") from e
    with zf:
        names = zf.namelist()
        by_base = {}
        for n in names:
            by_base.setdefault(n.rsplit("/", 1)[-1], n)
        if "content.json" in by_base:
            try:
                data = json.loads(zf.read(by_base["content.json"]).decode("utf-8"))
            except Exception as e:
                raise RuntimeError(f"content.json 解析失败：{e}") from e
            return _tree_from_json(data)
        if "content.xml" in by_base:
            try:
                el = ET.fromstring(zf.read(by_base["content.xml"]))
            except Exception as e:
                raise RuntimeError(f"content.xml 解析失败：{e}") from e
            return _tree_from_xml(el)
    raise RuntimeError("xmind 缺少 content.json / content.xml（无法定位主题树）")


def _tree_from_json(data) -> dict:
    if isinstance(data, list):
        if not data:
            raise RuntimeError("content.json 为空列表")
        data = data[0]
    if not isinstance(data, dict):
        raise RuntimeError(f"content.json 顶层类型异常：{type(data).__name__}")
    topic = data.get("rootTopic") or data.get("topic") or data
    if not isinstance(topic, dict) or ("title" not in topic and "children" not in topic):
        raise RuntimeError("content.json 未找到 rootTopic / topic 主题树")
    return _json_topic(topic)


def _json_topic(node: dict) -> dict:
    text = node.get("title")
    if text is None:
        text = ""
    kids = []
    ch = node.get("children")
    if isinstance(ch, dict):
        for key in ("attached", "detached", "summary"):
            lst = ch.get(key)
            if isinstance(lst, list):
                kids.extend(_json_topic(c) for c in lst if isinstance(c, dict))
    elif isinstance(ch, list):
        kids.extend(_json_topic(c) for c in ch if isinstance(c, dict))
    return {"text": _clean_title(text), "children": kids}


def _tree_from_xml(root_el) -> dict:
    root_topic = None
    for el in root_el.iter():
        if _local(el.tag) == "topic":
            root_topic = el
            break
    if root_topic is None:
        raise RuntimeError("content.xml 未找到 topic 元素")
    return _xml_topic(root_topic)


def _xml_title(el) -> str:
    for c in el:
        loc = _local(c.tag)
        if loc == "title":
            return _clean_title("".join(c.itertext()))
        if loc == "topic":
            continue  # 不越界取子 topic 的标题
        t = _xml_title(c)
        if t:
            return t
    return ""


def _xml_child_topics(el) -> list:
    """子 topic（跳过更深层 topic，避免把孙节点当子节点）。"""
    out = []
    for c in el:
        if _local(c.tag) == "topic":
            out.append(c)
        else:
            out.extend(_xml_child_topics(c))
    return out


def _xml_topic(el) -> dict:
    return {"text": _xml_title(el),
            "children": [_xml_topic(c) for c in _xml_child_topics(el)]}


# ---------------------------------------------------------------- SmartArt 解析

def parse_smartart(data: bytes) -> dict:
    """SmartArt dgm:dataModel XML（dgm:pt 节点 + dgm:cxn 连接 + dgm:t 文本）→ 节点树。
    节点=名字空间无关匹配（局部名）；根=无入边的节点；循环/孤儿节点容错（孤儿挂到根）。"""
    if isinstance(data, bytearray):
        data = bytes(data)
    try:
        root_el = ET.fromstring(data)
    except Exception as e:
        raise RuntimeError(f"SmartArt dataModel XML 解析失败：{e}") from e

    pts: dict[str, dict] = {}
    order: list[str] = []
    for el in root_el.iter():
        if _local(el.tag) != "pt":
            continue
        pid = el.get("modelId") or el.get("id")
        if not pid:
            pid = f"__auto_{len(order)}"
        pts[pid] = {"text": _smartart_text(el), "type": el.get("type") or "",
                    "order": len(order)}
        order.append(pid)
    if not pts:
        raise RuntimeError("SmartArt dataModel 无 dgm:pt 节点")

    raw_edges: list[tuple[str, str, str]] = []
    for el in root_el.iter():
        if _local(el.tag) != "cxn":
            continue
        src = el.get("srcId")
        dst = el.get("destId")
        typ = el.get("type") or ""
        for c in el:
            loc = _local(c.tag)
            if loc == "stId" and src is None:
                src = (c.text or c.get("val") or "").strip() or None
            elif loc == "endId" and dst is None:
                dst = (c.text or c.get("val") or "").strip() or None
        if src and dst:
            raw_edges.append((src, dst, typ))

    # 有 parOf（父子）连接时只用父子边，避免 presOf 等版式边混入树
    edges = [(s, d) for s, d, t in raw_edges if t == "parOf"]
    if not edges:
        edges = [(s, d) for s, d, _t in raw_edges]

    children: dict[str, list[str]] = {pid: [] for pid in pts}
    in_degree = {pid: 0 for pid in pts}
    for s, d in edges:
        if s not in pts or d not in pts or s == d:
            continue
        if d in children[s]:
            continue
        children[s].append(d)
        in_degree[d] += 1

    # 根 = 无入边的节点（优先 type=doc，其次文档序）
    cands = [pid for pid in order if in_degree[pid] == 0]
    if cands:
        root_id = next((p for p in cands if pts[p]["type"] == "doc"), cands[0])
    else:
        root_id = order[0]  # 全环：任取一点破环

    visited: set[str] = set()

    def build(pid: str) -> dict:
        visited.add(pid)
        kids = []
        for c in children.get(pid, ()):
            if c in visited:
                continue  # 环：忽略回边
            kids.append(build(c))
        return {"text": pts[pid]["text"], "children": kids}

    root = build(root_id)
    # 孤儿（含其它无入边候选、纯环残留）挂到根
    orphans = [p for p in order if p not in visited]
    for p in orphans:
        if p in visited:
            continue
        root["children"].append(build(p))
    return root


def _smartart_text(el) -> str:
    parts = []
    for d in el.iter():
        if _local(d.tag) == "t":
            parts.append("".join(d.itertext()))
    return _clean_title("".join(parts))


# ---------------------------------------------------------------- 导图渲染

def _norm_tree(node) -> dict | None:
    if not isinstance(node, dict):
        return None
    text = node.get("text")
    if text is None:
        text = node.get("title", "")
    ch = node.get("children") or []
    if isinstance(ch, dict):
        seq: list = []
        for key in ("attached", "detached", "summary"):
            lst = ch.get(key)
            if isinstance(lst, list):
                seq.extend(lst)
        ch = seq
    kids = []
    if isinstance(ch, list):
        for c in ch:
            n = _norm_tree(c)
            if n is not None:
                kids.append(n)
    return {"text": str(text), "children": kids}


def _walk(node, fn):
    fn(node)
    for c in node["children"]:
        _walk(c, fn)


def _count_leaves(node) -> int:
    if not node["children"]:
        node["_leaves"] = 1
        return 1
    n = sum(_count_leaves(c) for c in node["children"])
    node["_leaves"] = n
    return n


def _capsule_pts(cx: float, cy: float, w: float, h: float, seg: int = _ARC_SEG):
    """圆角胶囊（stadium）轮廓闭合折线：上下平边 + 两端半圆。"""
    import math
    r = h / 2.0
    hw = max(w / 2.0, r)
    rc_x = cx + hw - r
    lc_x = cx - hw + r
    pts = [(lc_x, cy - r), (rc_x, cy - r)]
    for i in range(1, seg):
        a = -math.pi / 2 + math.pi * i / seg
        pts.append((rc_x + r * math.cos(a), cy + r * math.sin(a)))
    pts.append((rc_x, cy + r))
    pts.append((lc_x, cy + r))
    for i in range(1, seg):
        a = math.pi / 2 + math.pi * i / seg
        pts.append((lc_x + r * math.cos(a), cy + r * math.sin(a)))
    pts.append((lc_x, cy - r))
    return pts


def _quad_bezier(p0, p1, p2, seg: int = _BEZIER_SEG):
    """二次贝塞尔折线化。"""
    out = []
    for i in range(seg + 1):
        t = i / seg
        u = 1.0 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def draw_tree(ctx, root: dict, x: float, y: float, w: float, h: float) -> int:
    """树 → 手绘思维导图笔画（画进 ctx.strokes，返回笔画数）。x,y=框左上 mm。
    布局：根节点居中偏左，子树向右层层展开（经典右向导图）；层级纵向下排；
    连接线=平滑曲线（二次贝塞尔折线化，起点到子节点中心）；
    节点=圆角胶囊轮廓 + 文字（写字字库），字号随框自适应（下限 1.6mm，仍放不下就截短文字加省略）；
    子树高度按叶子数分配；整体等比缩放入框。"""
    from layout import draw_char, char_advance_mm, expand_chars, Stroke

    tree = _norm_tree(root)
    if tree is None or w <= 0 or h <= 0:
        return 0
    n0 = len(ctx.strokes)

    # ---- 度量与层级
    def text_w(t: str, s: float) -> float:
        return sum(char_advance_mm(ctx, c, s) for c in expand_chars(t))

    _count_leaves(tree)
    nodes: list[dict] = []

    def prep(n, lvl):
        n["_lvl"] = lvl
        n["_size"] = max(_MIN_SIZE_MM, _DESIGN_ROOT_MM * (_LEVEL_SHRINK ** lvl))
        n["_tw"] = text_w(n["text"], n["_size"])
        n["_cap_h"] = _CAP_H_K * n["_size"]
        n["_cap_w"] = max(n["_tw"] + 2 * _CAP_PAD_K * n["_size"], n["_cap_h"])
        nodes.append(n)
        for c in n["children"]:
            prep(c, lvl + 1)

    prep(tree, 0)
    total_leaves = tree["_leaves"]
    max_cap_h = max(n["_cap_h"] for n in nodes)
    band = max_cap_h + _VGAP_K * _DESIGN_ROOT_MM

    # ---- 层列（同层胶囊左对齐，列宽取层内最大）
    depth = max(n["_lvl"] for n in nodes)
    col_w = [0.0] * (depth + 1)
    for n in nodes:
        col_w[n["_lvl"]] = max(col_w[n["_lvl"]], n["_cap_w"])
    hgap = _HGAP_K * _DESIGN_ROOT_MM
    col_x = [0.0] * (depth + 1)
    for d in range(1, depth + 1):
        col_x[d] = col_x[d - 1] + col_w[d - 1] + hgap
    nat_w = col_x[depth] + col_w[depth]
    nat_h = total_leaves * band

    # ---- 纵向：子树高度按叶子数分配，节点居其带中
    def place(n, y_top):
        span = n["_leaves"] * band
        n["_cy"] = y_top + span / 2.0
        n["_cx"] = col_x[n["_lvl"]] + n["_cap_w"] / 2.0
        yy = y_top
        for c in n["children"]:
            place(c, yy)
            yy += c["_leaves"] * band

    place(tree, 0.0)

    # ---- 整体等比缩放入框（居中）
    k = min(w / nat_w, h / nat_h) if nat_w > 0 and nat_h > 0 else 1.0
    k = min(k, _MAX_SCALE)
    off_x = x + (w - nat_w * k) / 2.0
    off_y = y + (h - nat_h * k) / 2.0

    # ---- 连接线（父右缘 → 子左缘，二次贝塞尔）
    def link(parent, child):
        p0 = (off_x + (parent["_cx"] + parent["_cap_w"] / 2.0) * k,
              off_y + parent["_cy"] * k)
        p2 = (off_x + (child["_cx"] - child["_cap_w"] / 2.0) * k,
              off_y + child["_cy"] * k)
        p1 = ((p0[0] + p2[0]) / 2.0, p0[1])
        ctx.strokes.append(Stroke(_quad_bezier(p0, p1, p2)))

    for n in nodes:
        for c in n["children"]:
            link(n, c)

    # ---- 节点胶囊 + 文字
    def fit_text(t: str, size: float, max_w: float) -> str:
        """放不下就截短加省略号。"""
        if max_w <= 0:
            return ""
        if text_w(t, size) <= max_w:
            return t
        ell = _ELLIPSIS
        if text_w(ell, size) > max_w:
            return ""
        for i in range(len(t) - 1, -1, -1):
            cand = t[:i] + ell
            if text_w(cand, size) <= max_w:
                return cand
        return ell

    for n in nodes:
        cx = off_x + n["_cx"] * k
        cy = off_y + n["_cy"] * k
        cap_w = n["_cap_w"] * k
        cap_h = n["_cap_h"] * k
        size = max(_MIN_SIZE_MM, n["_size"] * k)
        t = expand_chars(n["text"])
        inner = cap_w - 2 * _CAP_PAD_K * size
        t = fit_text(t, size, inner)
        ctx.strokes.append(Stroke(_capsule_pts(cx, cy, cap_w, cap_h)))
        tw = text_w(t, size)
        tx = cx - tw / 2.0
        baseline = cy + (_EM_TOP - _EM_BOTTOM) / 2.0 * (size / _EM_UNITS)
        for ch in t:
            tx += draw_char(ctx, ch, tx, baseline, size)

    return len(ctx.strokes) - n0
