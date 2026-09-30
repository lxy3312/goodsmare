"""アニメイト animate 通贩：抓搜索结果页，按"登録[新しい]"排、不显示販売終了。

这个站卖的是新品，不是二手：上新就是新登记的商品，多半是预约中的，所以"卖完了"指的是下架。
对照 2026 年 9 月的真实页面写的，请求参数都是页面搜索表单里的：
  ss=5    排序"登録[新しい]"；ss=1 是发售时间，会混进大量老商品
  sl=100  每页件数，页面只给 40 / 70 / 100
  nf=1    不带它，下面的 nd[] 筛选不生效
  nd[]=7  不显示"販売終了"
搜索表单没有价格区间和排除词，这两项由 monitor 里的 passes() 在本地过滤。
"""

from __future__ import annotations

import re

from .. import net
from ..htmlparse import parse
from .base import Item, NoResults, Query, Source, generic_items, img_src, parse_yen

SEARCH = "https://www.animate-onlineshop.jp/products/list.php"
BASE = "https://www.animate-onlineshop.jp/"
LINK = re.compile(r"/pd/(\d+)/")
EMPTY = ("に関する商品は0件",)
# 販売状況里表示买不到的几种写法（能买的有：予約受付中、在庫あり、残りわずか、取り寄せ、通常1～2日以内に入荷）
GONE = ("終了", "在庫なし", "在庫切れ", "品切れ", "完売")


def item_url(item_id: str) -> str:
    return f"{BASE}pd/{item_id}/"   # 末尾的斜杠不能省，没有它是 404


class Animate(Source):
    key = "animate"
    name = "animate"
    home = BASE

    def search(self, q: Query) -> list[Item]:
        params = {"smt": q.keyword, "ss": 5, "sl": 100, "nf": 1, "nd[]": 7}
        html = net.get(SEARCH, params=params).check("animate").text()
        return self.parse(html)

    @staticmethod
    def parse(html: str) -> list[Item]:
        root = parse(html)
        items: list[Item] = []
        seen: set[str] = set()
        for li in root.select(".item_list li"):
            a = li.select_one("h3 a[href]") or li.select_one(".item_list_thumb a[href]")
            m = LINK.search(a.get("href")) if a else None
            if not m or m.group(1) in seen:
                continue
            item_id = m.group(1)
            seen.add(item_id)
            img = li.select_one(".item_list_thumb img") or li.select_one("img")
            # 打折的商品：saleprice 是现价，oldprice 是原价；平时只有 price
            price_node = li.select_one(".saleprice") or li.select_one(".price")
            status_node = li.select_one(".stock span")
            status = status_node.text() if status_node else ""
            tag = li.select_one(".item_list_class")
            extra = " ".join(p for p in (status, "特典あり" if tag and "特典あり" in tag.text() else "") if p)
            items.append(Item(
                source="animate", id=item_id, title=(a.text() or (img.get("title") if img else "")),
                price=parse_yen(price_node.text()) if price_node else None,
                url=item_url(item_id), image=img_src(img, BASE),
                sold=any(w in status for w in GONE), extra=extra[:20],
            ))
        if items:
            return items
        if any(m in html for m in EMPTY):
            return NoResults()
        return generic_items(root, LINK, BASE, "animate", item_url)
