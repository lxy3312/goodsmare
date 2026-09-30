"""骏河屋 Suruga-ya：抓搜索结果页，按更新时间倒序、只看有货。

骏河屋一个"商品"下面挂着好几份库存：自家的中古/新品，外加第三方卖家（マケプレ）。
这里取能买到的最低价，并把来源写进附加说明。价格区块里还混着定价、限时特价前的原价、
运费说明（"お買上金額 5,000円未満…"），这些都不是售价，要跳过。

注意 inStock 参数是反着的：页面上写着"品切れ: ON | OFF"，inStock=On 是连售罄的一起显示，
inStock=Off 才是只看有货。以上都对照 2026 年 9 月的真实页面。
"""

from __future__ import annotations

import re
import urllib.parse

from .. import net
from ..htmlparse import Node, parse
from .base import Item, NoResults, Query, Source, generic_items, img_src, parse_yen

SEARCH = "https://www.suruga-ya.jp/search"
BASE = "https://www.suruga-ya.jp/"
LINK = re.compile(r"suruga-ya\.jp/product/(?:detail|other)/([0-9A-Za-z-]+)")
EMPTY = ("検索結果はありません", "該当する商品が見つかりませんでした", "に該当する商品はありません")
NOT_PRICE = ("定価", "通常価格", "送料", "お買上金額", "手数料")


def item_url(code: str) -> str:
    return f"https://www.suruga-ya.jp/product/detail/{code}"


def offers(card: Node) -> list[tuple[str, int]]:
    """这件商品现在能买到的价格：[(来源, 价格)]。一个都没有就是卖完了。"""
    out = []
    timesale = card.select_one(".timesales") is not None
    for p in card.select(".price_teika"):
        text = p.text()
        price = parse_yen(text)
        if price is None or text.startswith(NOT_PRICE):
            continue
        # "中古：￥1,580 税込" → 中古；限时特价那行不带前缀
        label = re.split(r"[：:]", text)[0].strip() if re.search(r"[：:]", text) else ""
        out.append((label or ("タイムセール" if timesale else ""), price))
    for box in card.select(".makeplaTit"):
        price = parse_yen(box.text())
        if price is not None:
            out.append(("マケプレ", price))
    if out:
        return out
    # 认不出的新写法：价格区块里挑像售价的行，说明文字一律跳过
    price_box = card.select_one(".item_price") or card
    for p in price_box.select("p"):
        text = p.text()
        price = parse_yen(text)
        if price is not None and not any(w in text for w in NOT_PRICE):
            out.append(("", price))
    return out


class Surugaya(Source):
    key = "surugaya"
    name = "骏河屋"
    home = BASE
    rev = 2   # 旧版会把运费说明、定价当成售价，存下的价格不能拿来判断降价

    def search(self, q: Query) -> list[Item]:
        params = {"category": "", "search_word": q.keyword,
                  "rankBy": "modificationTime:descending", "inStock": "Off"}
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

            found = offers(card)
            if found:
                label, price = min(found, key=lambda o: o[1])
                sold = False
            else:
                teika = next((p for p in card.select(".price_teika") if p.text().startswith("定価")), None)
                label, price, sold = "品切れ", parse_yen(teika.text()) if teika else None, True
            img = card.select_one("img")
            image = img_src(img, BASE) or f"https://www.suruga-ya.jp/database/pics_light/game/{code}.jpg"
            items.append(Item(source="surugaya", id=code, title=title, price=price,
                              url=item_url(code), image=image, sold=sold, extra=label[:12]))
        if items:
            return items
        if any(m in html for m in EMPTY):
            return NoResults()
        return generic_items(root, LINK, BASE, "surugaya", item_url)
