"""乐天 Rakuma（fril.jp）：抓搜索结果页，按上架时间倒序、只看在售。"""

from __future__ import annotations

import re

from .. import net
from ..htmlparse import parse
from .base import (Item, NoResults, Query, Source, generic_items, img_src, looks_sold,
                   parse_yen, to_int)

SEARCH = "https://fril.jp/s"
LINK = re.compile(r"item\.fril\.jp/([0-9A-Za-z]+)")
EMPTY = ("該当する商品が見つかりません", "検索結果はありません", "に一致する商品は見つかりませんでした")


def item_url(item_id: str) -> str:
    return f"https://item.fril.jp/{item_id}"


class Rakuma(Source):
    key = "rakuma"
    name = "乐天Rakuma"
    home = "https://fril.jp/"

    def search(self, q: Query) -> list[Item]:
        params = {"query": q.keyword, "sort": "created_at", "order": "desc",
                  "transaction": "selling",
                  "min": q.price_min or None, "max": q.price_max or None}
        html = net.get(SEARCH, params=params).check("Rakuma").text()
        return self.parse(html)

    @staticmethod
    def parse(html: str) -> list[Item]:
        root = parse(html)
        items: list[Item] = []
        seen: set[str] = set()
        for box in root.select(".item-box"):
            a = box.select_one("a.link_search_title, a.link_search_image, a[href*=item.fril.jp]")
            m = LINK.search(a.get("href")) if a else None
            if not m or m.group(1) in seen:
                continue
            item_id = m.group(1)
            seen.add(item_id)
            name = box.select_one(".item-box__item-name")
            img = box.select_one("img")
            title = (name.text() if name else "") or (img.get("alt") if img else "")
            price_node = box.select_one("[itemprop=price]")
            price = None
            if price_node is not None:
                price = to_int(price_node.get("data-content") or price_node.get("content")
                               or price_node.text())
            if price is None:
                p = box.select_one(".item-box__item-price")
                price = parse_yen(p.text()) if p else parse_yen(box.text())
            sold = bool(box.select_one(".item-box__soldout_ribbon")) or looks_sold(
                box.text().replace(title, ""))
            items.append(Item(source="rakuma", id=item_id, title=title, price=price,
                              url=item_url(item_id), image=img_src(img), sold=sold))
        if items:
            return items
        if any(m in html for m in EMPTY):
            return NoResults()
        return generic_items(root, LINK, "https://fril.jp/", "rakuma", item_url)
