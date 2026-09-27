"""雅虎闲置 Yahoo!フリマ（原 PayPayフリマ）：网页前端自己调的 JSON 搜索接口。

字段名以后可能变，所以不写死路径：在返回的 JSON 里找"有 id、有标题、有价格"的对象。
接口不通时退回搜索页的 __NEXT_DATA__ / 商品链接。
"""

from __future__ import annotations

import re
import urllib.parse

from .. import net
from ..htmlparse import parse
from .base import Item, NoResults, Query, Source, generic_items, next_data, to_int, walk_json

API = "https://paypayfleamarket.yahoo.co.jp/api/v1/search"
BASE = "https://paypayfleamarket.yahoo.co.jp"
LINK = re.compile(r"paypayfleamarket\.yahoo\.co\.jp/item/([A-Za-z0-9]+)")


def item_url(item_id: str) -> str:
    return f"{BASE}/item/{item_id}"


def _looks_like_item(d: dict) -> bool:
    return "id" in d and "title" in d and "price" in d


def _image(d: dict) -> str:
    for k in ("thumbnailImageUrl", "thumbnailUrl", "imageUrl", "image"):
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
    imgs = d.get("images") or d.get("thumbnails")
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        return first if isinstance(first, str) else (first.get("url") or first.get("src") or "")
    return ""


def items_from_json(data) -> list[Item]:
    items, seen = [], set()
    for d in walk_json(data, _looks_like_item):
        item_id = str(d["id"])
        if item_id in seen:
            continue
        seen.add(item_id)
        status = str(d.get("itemStatus") or d.get("status") or "").upper()
        items.append(Item(
            source="yahoo_flea", id=item_id, title=str(d.get("title") or ""),
            price=to_int(d.get("price")), url=item_url(item_id), image=_image(d),
            sold=status in ("SOLD", "SOLD_OUT", "CLOSED", "TRADING"),
        ))
    return items


class YahooFlea(Source):
    key = "yahoo_flea"
    name = "雅虎闲置"
    home = BASE + "/"

    def search(self, q: Query) -> list[Item]:
        params = {"query": q.keyword, "results": 100, "imageShape": "square",
                  "sort": "openTime", "order": "desc", "webp": "false",
                  "module": "catalog:hit:21", "itemStatus": "open"}
        headers = {"Accept": "application/json", "Referer": BASE + "/"}
        err = None
        try:
            resp = net.get(API, params=params, headers=headers).check("雅虎闲置")
            data = resp.json()
            items = items_from_json(data)
            if items:
                return items
            if isinstance(data, dict) and (data.get("totalResultsAvailable") == 0 or data.get("items") == []):
                return NoResults()
        except (net.FetchError, ValueError) as e:
            if isinstance(e, net.Blocked):
                raise
            err = e
        # 接口不通就看网页版
        url = f"{BASE}/search/{urllib.parse.quote(q.keyword)}"
        page = net.get(url, params={"open": 1, "sort": "openTime", "order": "desc"})
        if not page.ok and err:
            raise err
        return self.parse_page(page.check("雅虎闲置").text())

    @staticmethod
    def parse_page(html: str) -> list[Item]:
        root = parse(html)
        data = next_data(root)
        if data:
            items = items_from_json(data)
            if items:
                return items
        return generic_items(root, LINK, BASE + "/", "yahoo_flea", item_url)
