"""中古店 Mandarake（まんだらけ）：通贩站搜索页，按入荷顺倒序、隐藏售罄。

选择器对照开源项目 mdrscr 的解析写的。成人向商品会隐藏详情链接，只给一个带编号的元素。
"""

from __future__ import annotations

import re

from .. import net
from ..htmlparse import parse
from .base import Item, NoResults, Query, Source, generic_items, img_src, parse_yen

SEARCH = "https://order.mandarake.co.jp/order/listPage/list"
BASE = "https://order.mandarake.co.jp/"
LINK = re.compile(r"itemCode=([0-9A-Za-z]+)")
EMPTY = ("該当する商品はありませんでした", "検索結果はありません", "該当する商品がありません")


def item_url(code: str) -> str:
    return f"https://order.mandarake.co.jp/order/detailPage/item?itemCode={code}"


class Mandarake(Source):
    key = "mandarake"
    name = "Mandarake"
    home = BASE

    def search(self, q: Query) -> list[Item]:
        params = {"keyword": q.keyword, "dispAdult": 0, "soldOut": 1, "sort": "arrival",
                  "sortOrder": 1, "dispCount": 48, "lang": "ja",
                  "maxPrice": q.price_max or None}
        html = net.get(SEARCH, params=params).check("Mandarake").text()
        return self.parse(html)

    @staticmethod
    def parse(html: str) -> list[Item]:
        root = parse(html)
        items: list[Item] = []
        seen: set[str] = set()
        for block in root.select(".thumlarge .block") or root.select(".block"):
            if block.select_one(".title") is None:
                continue
            link = block.select_one(".pic a[href], .title a[href]")
            code = ""
            if link is not None:
                m = LINK.search(link.get("href"))
                code = m.group(1) if m else ""
            if not code:
                adult = block.select_one(".adult_link[id]")
                code = adult.get("id").strip() if adult else ""
            if not code or code in seen:
                continue
            seen.add(code)
            t = block.select_one(".title a") or block.select_one(".title p") or block.select_one(".title")
            price_node = block.select_one(".price")
            shop = block.select_one(".basic .shop, .shop")
            stock = block.select_one(".basic .stock, .stock")
            stock_text = stock.text() if stock else ""
            img = block.select_one(".pic img") or block.select_one("img")
            items.append(Item(
                source="mandarake", id=code, title=t.text() if t else "",
                price=parse_yen(price_node.text()) if price_node else None,
                url=item_url(code), image=img_src(img, BASE),
                sold=("売切" in stock_text or "SOLD" in stock_text.upper()),
                extra=(shop.text() if shop else "")[:20],
            ))
        if items:
            return items
        if any(m in html for m in EMPTY):
            return NoResults()
        return generic_items(root, LINK, BASE, "mandarake", item_url)
