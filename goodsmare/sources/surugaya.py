"""骏河屋 Suruga-ya：抓搜索结果页，按更新时间倒序、只看有货。

骏河屋一个"商品"下面挂着好几份库存（中古/新品/マケプレ），列表里给的是各自最低价。
这里取其中最便宜的那个作为价格，并把来源写进附加说明。
"""

from __future__ import annotations

import re
import urllib.parse

from .. import net
from ..htmlparse import parse
from .base import Item, NoResults, Query, Source, generic_items, img_src, parse_yen

SEARCH = "https://www.suruga-ya.jp/search"
BASE = "https://www.suruga-ya.jp/"
LINK = re.compile(r"suruga-ya\.jp/product/(?:detail|other)/([0-9A-Za-z-]+)")
EMPTY = ("検索結果はありません", "該当する商品が見つかりませんでした", "に該当する商品はありません")


def item_url(code: str) -> str:
    return f"https://www.suruga-ya.jp/product/detail/{code}"


class Surugaya(Source):
    key = "surugaya"
    name = "骏河屋"
    home = BASE

    def search(self, q: Query) -> list[Item]:
        params = {"category": "", "search_word": q.keyword,
                  "rankBy": "modificationTime:descending", "inStock": "On"}
        html = net.get(SEARCH, params=params).check("骏河屋").text()
        return self.parse(html)

    @staticmethod
    def parse(html: str) -> list[Item]:
        root = parse(html)
        items: list[Item] = []
        seen: set[str] = set()
        for card in root.select("div.item"):
            link = card.select_one(".title a[href], .thum a[href], .photo_box a[href], a[href*=/product/]")
            m = LINK.search(urllib.parse.urljoin(BASE, link.get("href"))) if link else None
            if not m or m.group(1) in seen:
                continue
            code = m.group(1)
            seen.add(code)
            title_node = card.select_one(".title a, .title, .product-name")
            title = title_node.text() if title_node else link.text()

            best, label = None, ""
            for row in card.select(".price_teika, .item_price p, .price"):
                text = row.text()
                price = parse_yen(text)
                if price is not None and (best is None or price < best):
                    best = price
                    # "中古：￥1,200 税込" → "中古"
                    label = re.split(r"[：:]", text)[0].strip() if re.search(r"[：:]", text) else ""
            if best is None:
                best = parse_yen(card.text())
            img = card.select_one("img")
            image = img_src(img, BASE) or f"https://www.suruga-ya.jp/database/pics_light/game/{code}.jpg"
            items.append(Item(source="surugaya", id=code, title=title, price=best,
                              url=item_url(code), image=image, extra=label[:12]))
        if items:
            return items
        if any(m in html for m in EMPTY):
            return NoResults()
        return generic_items(root, LINK, BASE, "surugaya", item_url)
