"""一个够用的小 DOM：标准库 HTMLParser 建树，再配一个只认后代选择器的 select()。

支持的选择器写法：`tag`、`.cls`、`tag.a.b`、`[attr]`、`[attr=value]`、`[attr*=value]`，
用空格连成后代关系，逗号分隔多个备选。够对付商品列表页了，不打算做成完整 CSS 引擎。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
RAW = {"script", "style"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent: "Node | None"):
        self.tag = tag
        self.attrs = attrs
        self.children: list = []
        self.parent = parent

    def get(self, name: str, default: str = "") -> str:
        v = self.attrs.get(name)
        return default if v is None else v

    @property
    def classes(self) -> set:
        return set(self.attrs.get("class", "").split())

    def iter(self):
        """所有后代元素（深度优先、文档顺序），不含自己。"""
        stack = list(reversed([c for c in self.children if isinstance(c, Node)]))
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed([c for c in node.children if isinstance(c, Node)]))

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def text(self) -> str:
        parts: list[str] = []
        self._collect(parts)
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def _collect(self, parts: list) -> None:
        for c in self.children:
            if isinstance(c, str):
                parts.append(c)
            elif c.tag not in RAW:
                block = c.tag in ("br", "p", "div", "li", "dd", "dt", "td")
                if block:
                    parts.append(" ")
                c._collect(parts)
                if block:
                    parts.append(" ")

    def raw_text(self) -> str:
        """script/style 里的原文。"""
        return "".join(c for c in self.children if isinstance(c, str))

    def select(self, selector: str) -> list["Node"]:
        out: list[Node] = []
        seen: set[int] = set()
        for alt in selector.split(","):
            for n in self._select_chain([_parse_simple(p) for p in alt.split()]):
                if id(n) not in seen:
                    seen.add(id(n))
                    out.append(n)
        return out

    def select_one(self, selector: str) -> "Node | None":
        for alt in selector.split(","):
            found = self._select_chain([_parse_simple(p) for p in alt.split()])
            if found:
                return found[0]
        return None

    def _select_chain(self, chain) -> list["Node"]:
        current = [self]
        for simple in chain:
            nxt: list[Node] = []
            seen: set[int] = set()
            for root in current:
                for n in root.iter():
                    if id(n) not in seen and _match(n, simple):
                        seen.add(id(n))
                        nxt.append(n)
            current = nxt
            if not current:
                break
        return current

    def __repr__(self) -> str:
        return f"<{self.tag} {self.attrs}>"


_SIMPLE = re.compile(r"^([a-zA-Z0-9]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$")
_ATTR = re.compile(r"\[([\w:-]+)(?:([*^$]?=)[\"']?([^\"'\]]*)[\"']?)?\]")


def _parse_simple(s: str):
    m = _SIMPLE.match(s.strip())
    if not m:
        raise ValueError(f"不支持的选择器：{s}")
    tag = m.group(1).lower()
    classes = [c for c in m.group(2).split(".") if c]
    attrs = _ATTR.findall(m.group(3))
    return tag, classes, attrs


def _match(node: Node, simple) -> bool:
    tag, classes, attrs = simple
    if tag and node.tag != tag:
        return False
    if classes:
        have = node.classes
        if not all(c in have for c in classes):
            return False
    for name, op, value in attrs:
        v = node.attrs.get(name)
        if v is None:
            return False
        if op == "=" and v != value:
            return False
        if op == "*=" and value not in v:
            return False
        if op == "^=" and not v.startswith(value):
            return False
        if op == "$=" and not v.endswith(value):
            return False
    return True


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)

    def handle_endtag(self, tag):
        # 容错：往上找同名标签再关；找不到就当它不存在。
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root
