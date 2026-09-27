"""解析测试。样例页面按各站真实结构（及开源解析器里用的选择器）手写，不是抓来的原页面。"""

import json
import unittest

from goodsmare.sources.base import NoResults, parse_yen
from goodsmare.sources.mandarake import Mandarake
from goodsmare.sources.mercari import Mercari
from goodsmare.sources.rakuma import Rakuma
from goodsmare.sources.surugaya import Surugaya
from goodsmare.sources.yahoo_auction import YahooAuction
from goodsmare.sources.yahoo_flea import YahooFlea, items_from_json

YAHOO = """
<ul class="Products__items">
<li class="Product">
  <div class="Product__image"><a class="Product__imageLink" href="https://auctions.yahoo.co.jp/jp/auction/x1234567890">
    <img class="Product__imageData" src="https://auc-pctr.c.yimg.jp/i-img1200x900-1700000000abc.jpg"></a></div>
  <div class="Product__detail">
    <h3 class="Product__title"><a class="Product__titleLink" data-auction-id="x1234567890"
      data-auction-title="呪術廻戦 五条悟 缶バッジ" data-auction-img="https://auc-pctr.c.yimg.jp/i-img1200x900-1700000000abc.jpg"
      data-cl-params="_cl_link:title;etm=1700600000,stm=1700000000"
      href="https://auctions.yahoo.co.jp/jp/auction/x1234567890">呪術廻戦 五条悟 缶バッジ</a></h3>
    <div class="Product__priceInfo"><span class="Product__priceValue u-textRed">1,200円</span></div>
    <div class="Product__bonus" data-auction-id="x1234567890" data-auction-price="1200"
      data-auction-buynowprice="3000" data-auction-startprice="1000"></div>
    <dl><dt>入札</dt><dd class="Product__bid">3</dd></dl>
  </div>
</li>
<li class="Product">
  <div class="Product__detail">
    <h3><a class="Product__titleLink" href="https://auctions.yahoo.co.jp/jp/auction/b1111111111">夏油傑 アクスタ</a></h3>
    <div class="Product__priceInfo"><span class="Product__priceValue">800円</span></div>
  </div>
</li>
</ul>"""

MERCARI = {
    "meta": {"numFound": "2"},
    "items": [
        {"id": "m12345678901", "sellerId": "1", "status": "ITEM_STATUS_ON_SALE", "name": "五条悟 缶バッジ",
         "price": "1500", "created": "1700000000", "thumbnails": ["https://static.mercdn.net/thumb/m1.jpg"],
         "itemType": "ITEM_TYPE_MERCARI"},
        {"id": "2abcDEF", "name": "ショップの商品", "price": "2000", "itemType": "ITEM_TYPE_BEYOND",
         "shopName": "アニメ屋", "thumbnails": [], "status": "ITEM_STATUS_ON_SALE"},
        {"id": "m999", "name": "オークション", "price": "300", "itemType": "ITEM_TYPE_MERCARI",
         "auction": {"id": "a1", "totalBid": "2"}, "status": "ITEM_STATUS_ON_SALE"},
    ],
}

RAKUMA = """
<div class="view view_grid">
<div class="item">
 <div class="item-box">
  <div class="item-box__image-wrapper"><a class="link_search_image" href="https://item.fril.jp/0a1b2c3d4e5f60718293a4b5c6d7e8f9">
    <img alt="初音ミク フィギュア" class="img-responsive lazy" data-original="https://img.fril.jp/img/1/m/1.jpg?1" src="https://web-assets.fril.jp/blank.gif"></a></div>
  <div class="item-box__text-wrapper">
   <p class="item-box__item-name"><a class="link_search_title" href="https://item.fril.jp/0a1b2c3d4e5f60718293a4b5c6d7e8f9"><span>初音ミク フィギュア</span></a></p>
   <div class="item-box__item-price"><p><span data-content="JPY" itemprop="priceCurrency">¥</span><span data-content="3000" itemprop="price">3,000</span></p></div>
  </div>
 </div>
</div>
<div class="item">
 <div class="item-box">
  <div class="item-box__soldout_ribbon"></div>
  <a class="link_search_title" href="https://item.fril.jp/ffffffffffffffffffffffffffffffff"><span>売れた商品</span></a>
  <span itemprop="price" data-content="500">500</span>
 </div>
</div>
</div>"""

SURUGAYA = """
<div id="search_result">
<div class="item">
 <div class="thum"><a href="/product/detail/602123456"><img src="https://www.suruga-ya.jp/database/pics_light/game/602123456.jpg"></a></div>
 <div class="item_detail">
  <p class="title"><a href="/product/detail/602123456">缶バッジ 五条悟 「呪術廻戦」</a></p>
  <div class="item_price">
   <p class="price_teika">中古：<span class="text-red"><strong>￥1,580</strong></span> 税込</p>
   <p class="price_teika">新品：<span class="text-red"><strong>￥2,200</strong></span> 税込</p>
  </div>
 </div>
</div>
<div class="item">
 <div class="thum"><a href="https://www.suruga-ya.jp/product/detail/ZHOREI9999"><img data-src="/database/photo.php?shinaban=ZHOREI9999"></a></div>
 <p class="title"><a href="https://www.suruga-ya.jp/product/detail/ZHOREI9999">アクリルスタンド 虎杖悠仁</a></p>
 <div class="item_price"><p class="mgnB5"><a href="/product/other/ZHOREI9999"><span class="text-red">￥980</span> (3点の中古品とマケプレ)</a></p></div>
</div>
</div>"""

MANDARAKE = """
<div class="entry"><div class="thumlarge">
<div class="block">
  <div class="pic"><a href="/order/detailPage/item?itemCode=1234567890&amp;ref=list"><img src="https://img.mandarake.co.jp/webshopimg/1.jpg"></a></div>
  <div class="title"><p><a href="/order/detailPage/item?itemCode=1234567890&amp;ref=list">呪術廻戦 缶バッジ 五条悟</a></p></div>
  <div class="basic"><p class="shop">中野店</p><p class="itemno">nkn-01-123 (1234567890)</p><p class="stock">在庫あります</p></div>
  <div class="price"><p>1,100円(税込)</p></div>
</div>
<div class="block">
  <div class="pic"><div class="r18item"><img src="https://img.mandarake.co.jp/r18.jpg"></div>
    <div class="adult_link" id="9876543210"></div></div>
  <div class="title"><p>成人向け 同人誌</p></div>
  <div class="basic"><p class="shop">福岡店</p><p class="stock">在庫確認します</p></div>
  <div class="price"><p>500円</p></div>
</div>
</div></div>"""


class ParserTests(unittest.TestCase):
    def test_parse_yen(self):
        self.assertEqual(parse_yen("中古：￥1,580 税込"), 1580)
        self.assertEqual(parse_yen("1,100円(税込)"), 1100)
        self.assertEqual(parse_yen("¥ 3,000"), 3000)
        self.assertIsNone(parse_yen("価格なし"))

    def test_yahoo_auction(self):
        items = YahooAuction.parse(YAHOO)
        self.assertEqual([i.id for i in items], ["x1234567890", "b1111111111"])
        first = items[0]
        self.assertEqual(first.title, "呪術廻戦 五条悟 缶バッジ")
        self.assertEqual(first.price, 1200)
        self.assertIn("3人出价", first.extra)
        self.assertIn("一口价¥3,000", first.extra)
        self.assertTrue(first.image.endswith("abc.jpg"))
        self.assertEqual(items[1].price, 800)
        self.assertEqual(items[1].url, "https://auctions.yahoo.co.jp/jp/auction/b1111111111")

    def test_yahoo_auction_empty(self):
        items = YahooAuction.parse("<p>条件に一致する商品は見つかりませんでした。</p>")
        self.assertIsInstance(items, NoResults)

    def test_yahoo_auction_fallback(self):
        html = """<div><section><a href="https://auctions.yahoo.co.jp/jp/auction/q5555555555">
          <img src="https://x/1.jpg"><h2>新デザインの商品</h2></a><div>現在 2,345円</div></section>
          <section><a href="https://auctions.yahoo.co.jp/jp/auction/q6666666666">別の</a><div>900円</div></section></div>"""
        items = YahooAuction.parse(html)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "新デザインの商品")
        self.assertEqual(items[0].price, 2345)
        self.assertEqual(items[1].price, 900)

    def test_mercari(self):
        items = Mercari.parse(MERCARI)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].url, "https://jp.mercari.com/item/m12345678901")
        self.assertEqual(items[0].price, 1500)
        self.assertEqual(items[0].created, 1700000000)
        self.assertEqual(items[1].url, "https://jp.mercari.com/shops/product/2abcDEF")
        self.assertIn("アニメ屋", items[1].extra)
        self.assertIn("拍卖", items[2].extra)
        self.assertIsInstance(Mercari.parse({"items": []}), NoResults)

    def test_mercari_request_body(self):
        from goodsmare.sources.base import Query
        body = Mercari().body(Query("五条悟", price_max=3000, exclude=["空箱"]))
        cond = body["searchCondition"]
        self.assertEqual(cond["sort"], "SORT_CREATED_TIME")
        self.assertEqual(cond["priceMax"], 3000)
        self.assertEqual(cond["excludeKeyword"], "空箱")
        json.dumps(body)

    def test_mercari_dpop(self):
        import base64
        tok = Mercari()._dpop("https://api.mercari.jp/v2/entities:search", "POST")
        header, payload, sig = tok.split(".")
        h = json.loads(base64.urlsafe_b64decode(header + "=="))
        p = json.loads(base64.urlsafe_b64decode(payload + "=="))
        self.assertEqual(h["alg"], "ES256")
        self.assertEqual(h["typ"], "dpop+jwt")
        self.assertEqual(p["htm"], "POST")
        self.assertEqual(len(base64.urlsafe_b64decode(sig + "==")), 64)

    def test_yahoo_flea_json(self):
        data = {"totalResultsAvailable": 2, "items": [
            {"id": "z123456789", "title": "五条悟 ぬいぐるみ", "price": 2500,
             "thumbnailImageUrl": "https://auctions.c.yimg.jp/1.jpg", "itemStatus": "OPEN"},
            {"id": "z987654321", "title": "売れた", "price": 100, "itemStatus": "SOLD"}]}
        items = items_from_json(data)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, "https://paypayfleamarket.yahoo.co.jp/item/z123456789")
        self.assertEqual(items[0].image, "https://auctions.c.yimg.jp/1.jpg")
        self.assertFalse(items[0].sold)
        self.assertTrue(items[1].sold)

    def test_yahoo_flea_page(self):
        nd = {"props": {"pageProps": {"search": {"items": [
            {"id": "z1", "title": "A", "price": 10, "images": [{"url": "https://i/1.jpg"}]}]}}}}
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
        items = YahooFlea.parse_page(html)
        self.assertEqual(items[0].id, "z1")
        self.assertEqual(items[0].image, "https://i/1.jpg")

    def test_rakuma(self):
        items = Rakuma.parse(RAKUMA)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "初音ミク フィギュア")
        self.assertEqual(items[0].price, 3000)
        self.assertEqual(items[0].image, "https://img.fril.jp/img/1/m/1.jpg?1")
        self.assertFalse(items[0].sold)
        self.assertTrue(items[1].sold)

    def test_rakuma_generic_fallback(self):
        html = """<ul><li><a href="https://item.fril.jp/abc123"><img src="https://img.fril.jp/1.jpg" alt="x">
            <p>初音ミク フィギュア</p><span>¥2,500</span></a></li>
          <li><a href="https://item.fril.jp/def456"><p>SOLDIER フィギュア</p><span>¥800</span></a></li>
          <li><a href="https://item.fril.jp/ghi789"><p>売れた</p><span>¥100</span></a><span>SOLD</span></li></ul>"""
        items = Rakuma.parse(html)
        self.assertEqual([i.id for i in items], ["abc123", "def456", "ghi789"])
        self.assertEqual(items[0].title, "初音ミク フィギュア")
        self.assertEqual(items[0].price, 2500)
        self.assertEqual(items[0].image, "https://img.fril.jp/1.jpg")
        self.assertFalse(items[1].sold, "标题里的 SOLDIER 不能当成已售")
        self.assertTrue(items[2].sold)

    def test_surugaya(self):
        items = Surugaya.parse(SURUGAYA)
        self.assertEqual([i.id for i in items], ["602123456", "ZHOREI9999"])
        self.assertEqual(items[0].price, 1580)
        self.assertEqual(items[0].extra, "中古")
        self.assertEqual(items[0].title, "缶バッジ 五条悟 「呪術廻戦」")
        self.assertEqual(items[1].price, 980)
        self.assertEqual(items[1].image, "https://www.suruga-ya.jp/database/photo.php?shinaban=ZHOREI9999")

    def test_mandarake(self):
        items = Mandarake.parse(MANDARAKE)
        self.assertEqual([i.id for i in items], ["1234567890", "9876543210"])
        self.assertEqual(items[0].price, 1100)
        self.assertEqual(items[0].extra, "中野店")
        self.assertEqual(items[0].title, "呪術廻戦 缶バッジ 五条悟")
        self.assertEqual(items[0].url, "https://order.mandarake.co.jp/order/detailPage/item?itemCode=1234567890")
        self.assertEqual(items[1].image, "https://img.mandarake.co.jp/r18.jpg")


if __name__ == "__main__":
    unittest.main()
