"""煤炉 Mercari：走官网前端用的 JSON 接口，每个请求要带 DPoP 签名头。

请求体照抄网页版（对照过开源客户端 mercapi 2026 年的版本）。
"""

from __future__ import annotations

import time
import uuid

from .. import net
from ..es256 import SigningKey, jws
from .base import Item, NoResults, Query, Source, to_int

API = "https://api.mercari.jp/v2/entities:search"


class Mercari(Source):
    key = "mercari"
    name = "煤炉"
    home = "https://jp.mercari.com/"

    def __init__(self):
        super().__init__()
        self._uuid = str(uuid.uuid4())

    def _dpop(self, url: str, method: str) -> str:
        # 每个请求换一把新密钥：npm 版 mercapi 默认就这么做，说一直用同一把会遇到 "stale key"。
        # 监控一跑就是几天，宁可每次多花几毫秒。
        key = SigningKey()
        payload = {"iat": int(time.time()), "jti": str(uuid.uuid4()),
                   "htu": url, "htm": method, "uuid": self._uuid}
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": key.jwk()}
        return jws(payload, header, key)

    def body(self, q: Query) -> dict:
        return {
            "userId": "",
            "config": {"responseToggles": ["QUERY_SUGGESTION_WEB_1"]},
            "pageSize": 120,
            "pageToken": "",
            "searchSessionId": uuid.uuid4().hex,
            "source": "BaseSerp",
            "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
            "thumbnailTypes": [],
            "searchCondition": {
                "keyword": q.keyword,
                "excludeKeyword": " ".join(q.exclude),
                "sort": "SORT_CREATED_TIME",
                "order": "ORDER_DESC",
                "status": ["STATUS_ON_SALE"],
                "sizeId": [], "categoryId": [], "brandId": [], "sellerId": [],
                "priceMin": q.price_min or 0,
                "priceMax": q.price_max or 0,
                "itemConditionId": [], "shippingPayerId": [], "shippingFromArea": [],
                "shippingMethod": [], "colorId": [], "hasCoupon": False, "attributes": [],
                "itemTypes": [], "skuIds": [], "shopIds": [], "excludeShippingMethodIds": [],
            },
            "serviceFrom": "suruga",
            "withItemBrand": True,
            "withItemSize": False,
            "withItemPromotions": True,
            "withItemSizes": True,
            "withShopname": True,
            "useDynamicAttribute": True,
            "withSuggestedItems": True,
            "withOfferPricePromotion": True,
            "withProductSuggest": True,
            "withParentProducts": False,
            "withProductArticles": True,
            "withSearchConditionId": False,
            "withAuction": True,
            "laplaceDeviceUuid": str(uuid.uuid4()),
        }

    def search(self, q: Query) -> list[Item]:
        headers = {"X-Platform": "web", "Accept": "application/json, text/plain, */*",
                   "Origin": "https://jp.mercari.com", "Referer": "https://jp.mercari.com/",
                   "DPoP": self._dpop(API, "POST")}
        data = net.post(API, json_body=self.body(q), headers=headers).check("煤炉").json()
        return self.parse(data)

    @staticmethod
    def parse(data: dict) -> list[Item]:
        raw = data.get("items") or []
        if not raw:
            return NoResults()
        items = []
        for it in raw:
            item_id = str(it.get("id") or "")
            if not item_id:
                continue
            shop = it.get("itemType") == "ITEM_TYPE_BEYOND" or bool(it.get("shop"))
            url = (f"https://jp.mercari.com/shops/product/{item_id}" if shop
                   else f"https://jp.mercari.com/item/{item_id}")
            price = None if it.get("isNoPrice") else to_int(it.get("price"))
            extra = []
            if shop:
                extra.append("Shops" + (f"·{it['shopName']}" if it.get("shopName") else ""))
            auction = it.get("auction") or None
            if auction:
                bids = to_int(auction.get("totalBid")) or 0
                extra.append(f"拍卖·{bids}人出价" if bids else "拍卖")
            thumbs = it.get("thumbnails") or []
            items.append(Item(
                source="mercari", id=item_id, title=str(it.get("name") or ""), price=price, url=url,
                image=thumbs[0] if thumbs else "",
                sold=str(it.get("status", "")).endswith(("SOLD_OUT", "TRADING")),
                extra=" ".join(extra), created=to_int(it.get("created")) or 0,
            ))
        return items
