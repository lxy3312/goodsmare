"""雅虎拍卖 Yahoo!オークション：抓搜索结果页，按"新着順"排。

旧版页面的商品链接上挂着 data-auction-* 属性（对照过开源项目 Yoku 的解析），最好用；
拿不到时退回 __NEXT_DATA__，再不行就按商品链接兜底。
"""

from __future__ import annotations

import re

from .. import net
from ..htmlparse import parse
from .base import (Item, NoResults, Query, Source, generic_items, img_src, next_data,
                   parse_yen, to_int, walk_json)

SEARCH = "https://auctions.yahoo.co.jp/search/search"
LINK = re.compile(r"auctions\.yahoo\.co\.jp/jp/auction/([A-Za-z0-9]+)")
EMPTY = ("条件に一致する商品は見つかりませんでした", "に一致する商品はありません")


def item_url(aid: str) -> str:
    return f"https://auctions.yahoo.co.jp/jp/auction/{aid}"


class YahooAuction(Source):
    key = "yahoo_auction"
    name = "雅虎拍卖"
    home = "https://auctions.yahoo.co.jp/"

    def search(self, q: Query) -> list[Item]:
        params = {"p": q.keyword, "s1": "new", "o1": "d", "n": 100,
                  "aucminprice": q.price_min or None, "aucmaxprice": q.price_max or None}
        html = net.get(SEARCH, params=params).check("雅虎拍卖").text()
        return self.parse(html)

    @staticmethod
    def parse(html: str) -> list[Item]:
        if any(m in html for m in EMPTY):
            return NoResults()
        root = parse(html)
        items: list[Item] = []
        seen: set[str] = set()

        for a in root.select("a.Product__titleLink"):
            aid = a.get("data-auction-id")
            if not aid:
                m = LINK.search(a.get("href"))
                aid = m.group(1) if m else ""
            if not aid or aid in seen:
                continue
            seen.add(aid)
            card = next((n for n in a.ancestors() if "Product" in n.classes), a.parent)
            bonus = card.select_one("[data-auction-price]") if card else None
            price = to_int(bonus.get("data-auction-price")) if bonus else None
            if price is None and card is not None:
                pv = card.select_one(".Product__priceValue")
                price = parse_yen(pv.text() + "円") if pv else None
            buynow = to_int(bonus.get("data-auction-buynowprice")) if bonus else None
            extra = []
            bid = card.select_one(".Product__bid") if card else None
            if bid and bid.text().strip().isdigit():
                extra.append(f"{bid.text().strip()}人出价")
            if buynow:
                extra.append(f"一口价¥{buynow:,}")
            image = a.get("data-auction-img") or img_src(card.select_one("img") if card else None)
            items.append(Item(
                source="yahoo_auction", id=aid,
                title=a.get("data-auction-title") or a.get("title") or a.text(),
                price=price, url=item_url(aid), image=image, extra=" ".join(extra),
            ))
        if items:
            return items

        data = next_data(root)
        if data:
            for d in walk_json(data, lambda d: "auctionId" in d and "title" in d):
                aid = str(d["auctionId"])
                if aid in seen:
                    continue
                seen.add(aid)
                image = d.get("imageUrl") or d.get("thumbnailUrl") or d.get("image") or ""
                if isinstance(image, dict):
                    image = image.get("url", "")
                price = to_int(d.get("price") or d.get("currentPrice"))
                items.append(Item(source="yahoo_auction", id=aid, title=str(d["title"]),
                                  price=price, url=item_url(aid), image=str(image)))
            if items:
                return items

        return generic_items(root, LINK, "https://auctions.yahoo.co.jp/", "yahoo_auction", item_url)
