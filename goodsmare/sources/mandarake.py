"""中古店 Mandarake（まんだらけ）：通贩站搜索页，按入荷顺倒序、隐藏售罄。

选择器对照开源项目 mdrscr 的解析写的，2026 年 9 月又对过一次真实页面。
成人向商品会隐藏详情链接，只给一个带编号的元素。

没带 cookie 的请求会被 302 到官网首页，同时发一个 tr_mndrk_user cookie，带上它再来就正常了。
"""

from __future__ import annotations

import http.cookiejar
import re
import urllib.parse

from .. import net
from ..htmlparse import parse
from .base import Item, NoResults, Query, Source, generic_items, img_src, parse_yen

SEARCH = "https://order.mandarake.co.jp/order/listPage/list"
BASE = "https://order.mandarake.co.jp/"
LINK = re.compile(r"itemCode=([0-9A-Za-z]+)")
EMPTY = ("該当する商品はありませんでした", "検索結果はありません", "該当する商品がありません")
TAX_IN = re.compile(r"税込\s*([0-9][0-9,，]*)\s*円")


def item_url(code: str) -> str:
    return f"https://order.mandarake.co.jp/order/detailPage/item?itemCode={code}"


def price_of(text: str) -> int | None:
    """"2,000円 (税込 2,200円)" 取含税的 2200，和别的网站一样是实际要付的钱。"""
    m = TAX_IN.search(text)
    if m:
        return int(re.sub(r"[,，]", "", m.group(1)))
    return parse_yen(text)


class Mandarake(Source):
    key = "mandarake"
    name = "Mandarake"
    home = BASE

    def __init__(self):
        super().__init__()
        self.cookies = http.cookiejar.CookieJar()

    def search(self, q: Query) -> list[Item]:
        params = {"keyword": q.keyword, "dispAdult": 0, "soldOut": 1, "sort": "arrival",
                  "sortOrder": 1, "dispCount": 48, "lang": "ja",
                  "maxPrice": q.price_max or None}
        for _ in range(2):
            resp = net.get(SEARCH, params=params, cookies=self.cookies).check("Mandarake")
            if urllib.parse.urlsplit(resp.url).netloc == urllib.parse.urlsplit(BASE).netloc:
                return self.parse(resp.text())
            # 第一次会被跳回首页，cookie 这时已经拿到了
        raise net.FetchError("Mandarake 一直把搜索页跳回首页，可能改了访问规则，过一阵再试")

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
                price=price_of(price_node.text()) if price_node else None,
                url=item_url(code), image=img_src(img, BASE),
                sold=("売切" in stock_text or "SOLD" in stock_text.upper()),
                extra=(shop.text() if shop else "")[:20],
            ))
        if items:
            return items
        if any(m in html for m in EMPTY):
            return NoResults()
        return generic_items(root, LINK, BASE, "mandarake", item_url)
