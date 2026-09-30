"""各站适配器的共同部分：商品结构、查询条件，以及一个"按商品链接找卡片"的兜底解析。

页面改版是常态。每个站先用已知的 class 名解析；解析不出来时，退回到兜底：
找出所有指向商品详情页的链接，往上爬到"只包含这一件商品"的最大容器，
再从里面捞标题、图片、价格。这样 class 名变了，大多数情况下照样能用。
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from dataclasses import asdict, dataclass, field

from ..htmlparse import Node


@dataclass
class Item:
    source: str
    id: str
    title: str
    price: int | None
    url: str
    image: str = ""
    sold: bool = False
    extra: str = ""        # 附加说明：拍卖出价、店铺、成色……
    created: int = 0       # 上架时间（unix 秒），拿不到就是 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Query:
    keyword: str
    price_min: int = 0
    price_max: int = 0
    exclude: list = field(default_factory=list)


class Source:
    key = ""
    name = ""
    home = ""
    # 解析方式改得让旧记录不可信时（比如以前价格取错了）加一，监控会在这个站重新记一遍基线
    rev = 1

    def __init__(self):
        self.lock = threading.Lock()   # 同一个站同一时刻只发一个请求

    def search(self, q: Query) -> list[Item]:
        raise NotImplementedError


class NoResults(list):
    """站点明确说了"没有结果"：和"解析出 0 条"区分开，后者多半是页面改版。"""


_PRICE_RES = [
    re.compile(r"[¥￥]\s*([0-9][0-9,，]*)"),
    re.compile(r"([0-9][0-9,，]*)\s*円"),
]


def parse_yen(text: str) -> int | None:
    if not text:
        return None
    for rx in _PRICE_RES:
        m = rx.search(text)
        if m:
            digits = re.sub(r"[,，]", "", m.group(1))
            if digits.isdigit():
                return int(digits)
    return None


def to_int(v) -> int | None:
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def img_src(img: Node | None, base: str = "") -> str:
    if img is None:
        return ""
    for attr in ("data-original", "data-src", "data-lazy", "data-lazy-src", "src"):
        v = img.get(attr)
        if v and not v.startswith("data:") and "spacer" not in v and "blank" not in v:
            return urllib.parse.urljoin(base, v) if base else v
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        first = srcset.split(",")[0].split()[0]
        return urllib.parse.urljoin(base, first) if base else first
    return ""


def cards_by_link(root: Node, link_re: re.Pattern, base: str) -> list[tuple[str, Node, list[Node]]]:
    """兜底解析：返回 [(商品ID, 卡片容器, 指向它的链接们)]，按页面顺序。"""
    links: dict[str, list[Node]] = {}
    for a in root.select("a[href]"):
        href = urllib.parse.urljoin(base, a.get("href"))
        m = link_re.search(href)
        if m:
            links.setdefault(m.group(1), []).append(a)

    # 每个祖先节点下面挂着哪些商品：卡片就是"只挂着这一件"的最高祖先。
    ids_under: dict[int, set] = {}
    for item_id, anchors in links.items():
        for a in anchors:
            for anc in a.ancestors():
                ids_under.setdefault(id(anc), set()).add(item_id)

    out = []
    for item_id, anchors in links.items():
        card = anchors[0]
        for anc in anchors[0].ancestors():
            if anc.tag == "#root" or len(ids_under[id(anc)]) > 1:
                break
            card = anc
        out.append((item_id, card, anchors))
    return out


# 不用 \b：页面上常见 "¥100SOLD" 这种粘在一起的写法；也别把 SOLDIER 当成已售
_SOLD = re.compile(r"(?<![A-Za-z])SOLD(?![A-Za-z])|SOLD\s*OUT|売り切れ|売切|在庫なし", re.I)
_NOISE = re.compile(r"[¥￥]\s*[0-9][0-9,，]*|[0-9][0-9,，]*\s*円(?:\s*[（(]税込[)）])?|(?<![A-Za-z])SOLD(?:\s*OUT)?(?![A-Za-z])", re.I)


def looks_sold(text: str) -> bool:
    return bool(_SOLD.search(text or ""))


def generic_items(root: Node, link_re: re.Pattern, base: str, source: str, make_url) -> list[Item]:
    items = []
    for item_id, card, anchors in cards_by_link(root, link_re, base):
        # 链接里常常把价格、SOLD 标记也包进去了，去掉再当标题
        texts = (_NOISE.sub(" ", a.text()).strip() for a in anchors)
        title = max(texts, key=len, default="")
        img = card.select_one("img")
        if not title and img is not None:
            title = img.get("alt")
        if not title:
            title = card.get("title") or card.text()[:80]
        price = None
        for node in card.select("[itemprop=price]"):
            price = to_int(node.get("content") or node.get("data-content") or node.text())
            if price is not None:
                break
        text = card.text()
        if price is None:
            price = parse_yen(text)
        items.append(Item(
            source=source, id=item_id, title=title.strip(), price=price, url=make_url(item_id),
            image=img_src(img, base), sold=looks_sold(text.replace(title, "")),
        ))
    return items


def walk_json(obj, want) -> list[dict]:
    """在任意 JSON 里找"长得像商品"的 dict。want(d) 返回 True 就收下，不再往里钻。"""
    found = []
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if want(cur):
                found.append(cur)
                continue
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))
    return found


def next_data(root: Node):
    """Next.js 页面把数据塞在 <script id="__NEXT_DATA__"> 里。"""
    node = root.select_one("script[id=__NEXT_DATA__]")
    if node is None:
        return None
    try:
        return json.loads(node.raw_text())
    except ValueError:
        return None
