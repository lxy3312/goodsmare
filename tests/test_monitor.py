import tempfile
import unittest
from pathlib import Path
from unittest import mock

from goodsmare.config import ConfigStore
from goodsmare.monitor import Monitor, passes
from goodsmare.sources import SOURCES, Item, NoResults, Source
from goodsmare.store import Store


def item(i, price=1000, title=None, sold=False):
    return Item(source="mercari", id=f"m{i}", title=title or f"五条悟 缶バッジ {i}", price=price,
                url=f"https://jp.mercari.com/item/m{i}", sold=sold)


class FakeSource(Source):
    key = "mercari"
    name = "假煤炉"

    def __init__(self):
        super().__init__()
        self.pages = []
        self.queries = []

    def search(self, q):
        self.queries.append(q)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = ConfigStore(d / "config.json")
        self.store = Store(d / "goodsmare.db")
        self.src = FakeSource()
        self.patch = mock.patch.dict(SOURCES, {"mercari": self.src})
        self.patch.start()
        self.cfg.update(lambda c: c.update({
            "request_gap": 0.5, "auto_rate": False,
            "watches": [{"id": "w1", "keyword": "五条悟", "sources": ["mercari"]}]}))
        self.mon = Monitor(self.cfg, self.store, echo=False)
        self.mon._polite_wait = lambda *a: None

    def tearDown(self):
        self.patch.stop()
        self.store.db.close()
        self.tmp.cleanup()

    def cycle(self, page):
        self.src.pages.append(page)
        return self.mon.run_cycle()

    def status(self):
        return self.store.statuses()["w1"]["mercari"]

    def test_first_scan_is_baseline_then_new_items_hit(self):
        self.assertEqual(self.cycle([item(1), item(2)]), [])
        self.assertIn("首次扫描", self.status()["message"])
        hits = self.cycle([item(3), item(1), item(2)])
        self.assertEqual([h.item.id for h in hits], ["m3"])
        self.assertEqual(hits[0].kind, "new")
        self.assertEqual(self.store.feed()[0]["item_id"], "m3")
        # 同一件不会推第二次
        self.assertEqual(self.cycle([item(3), item(1)]), [])

    def test_filters(self):
        self.cfg.update(lambda c: c["watches"][0].update(
            {"price_max": 2000, "must": ["缶バッジ"], "exclude": ["まとめ"]}))
        self.cycle([item(1)])
        hits = self.cycle([
            item(2, price=5000),                       # 太贵
            item(3, title="五条悟 アクスタ"),            # 没有必含词
            item(4, title="五条悟 缶バッジ まとめ売り"),   # 有排除词
            item(5, sold=True),                        # 已售
            item(6, title="五条悟 ｶﾝﾊﾞｯｼﾞ 缶バッジ"),     # 正常
            item(1),
        ])
        self.assertEqual([h.item.id for h in hits], ["m6"])

    def test_price_drop(self):
        self.cfg.update(lambda c: c["watches"][0].update({"price_drop": True}))
        self.cycle([item(1, price=3000)])
        hits = self.cycle([item(1, price=2500)])
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0].kind, hits[0].old_price, hits[0].item.price), ("drop", 3000, 2500))
        self.assertEqual(self.cycle([item(1, price=2600)]), [])   # 涨价不提醒

    def test_changing_keyword_rebaselines(self):
        self.cycle([item(1)])
        self.cfg.update(lambda c: c["watches"][0].update({"keyword": "夏油傑"}))
        self.assertEqual(self.cycle([item(7), item(8)]), [])
        self.assertIn("首次扫描", self.status()["message"])
        self.assertEqual(len(self.cycle([item(9), item(7)])), 1)

    def test_suspicious_empty_does_not_set_baseline(self):
        self.cycle([])
        self.assertFalse(self.status()["ok"])
        self.assertIn("解析到 0 件", self.status()["message"])
        # 修好之后第一次拿到结果仍然只当基线，不会刷屏
        self.assertEqual(self.cycle([item(1), item(2)]), [])

    def test_no_results_is_fine(self):
        self.cycle(NoResults())
        self.assertTrue(self.status()["ok"])
        hits = self.cycle([item(1)])
        self.assertEqual(len(hits), 1)

    def test_flood_guard(self):
        self.cycle([item(i) for i in range(5)])
        hits = self.cycle([item(i) for i in range(100, 160)])
        self.assertEqual(hits, [])
        self.assertIn("换了排序", self.status()["message"])
        self.assertEqual(self.cycle([item(200)] + [item(i) for i in range(100, 160)])[0].item.id, "m200")

    def test_error_is_recorded_and_loop_survives(self):
        from goodsmare import net
        self.src.pages.append(net.Blocked("煤炉 拦下了请求"))
        self.assertEqual(self.mon.run_cycle(), [])
        st = self.status()
        self.assertFalse(st["ok"])
        self.assertIn("拦下", st["message"])

    def test_notifies_channels(self):
        self.cfg.update(lambda c: c.update({"channels": [{"type": "webhook", "url": "http://x/hook"}]}))
        self.cycle([item(1)])
        with mock.patch("goodsmare.notify.send") as send:
            self.cycle([item(2), item(1)])
        send.assert_called_once()
        self.assertEqual(send.call_args[0][1][0].item.id, "m2")

    def test_push_page_only_when_phone_can_open_it(self):
        from goodsmare.notify import Hit
        hits = [Hit(item(1), "w1", "五条悟")]
        self.assertEqual(self.mon.ctx(self.cfg.load(), hits).page_url, "", "网页服务没开（once 模式）")
        self.mon.serving = True
        self.assertEqual(self.mon.ctx(self.cfg.load(), hits).page_url, "", "只听本机，手机连不上")
        self.cfg.update(lambda c: c["web"].update({"public_url": "http://100.64.1.2:8787", "token": "t"}))
        ctx = self.mon.ctx(self.cfg.load(), hits)
        self.assertRegex(ctx.page_url, r"^http://100\.64\.1\.2:8787/p/[A-Za-z0-9_-]{16}$")
        self.assertEqual(ctx.web_url, "http://100.64.1.2:8787")
        page = self.store.page(ctx.page_url.rsplit("/", 1)[1])
        self.assertEqual(page["items"][0]["id"], "m1")
        self.assertEqual(self.mon.ctx(self.cfg.load()).page_url, "", "没有命中就不生成")
        self.cfg.update(lambda c: c["web"].update({"push_page": False}))
        self.assertEqual(self.mon.ctx(self.cfg.load(), hits).page_url, "", "用户选了直接去原商品页")

    def test_cycle_pushes_with_page(self):
        self.mon.serving = True
        self.cfg.update(lambda c: (c.update({"channels": [{"type": "webhook", "url": "http://x/hook"}]}),
                                   c["web"].update({"public_url": "http://pc:8787", "token": "t"})))
        self.cycle([item(1)])
        with mock.patch("goodsmare.notify.send") as send:
            self.cycle([item(2), item(1)])
        ctx = send.call_args[0][2]
        self.assertTrue(ctx.page_url.startswith("http://pc:8787/p/"))
        self.assertEqual(self.store.page(ctx.page_url.rsplit("/", 1)[1])["items"][0]["id"], "m2")

    def test_blocked_items_are_never_pushed(self):
        self.cfg.update(lambda c: c["watches"][0].update({"price_drop": True}))
        self.cycle([item(1), item(2)])
        self.store.block([("mercari", "m3", "w1"), ("mercari", "m2", "w1")])
        hits = self.cycle([item(3), item(2, price=500), item(1)])
        self.assertEqual(hits, [], "屏蔽过的，上新、降价都不推")
        self.assertEqual(self.store.feed(), [])
        hits = self.cycle([item(4), item(3), item(2, price=400), item(1)])
        self.assertEqual([h.item.id for h in hits], ["m4"])

    def test_test_push_uses_latest_real_item(self):
        self.cycle([item(1)])
        self.cycle([item(2), item(1)])
        hits, where = self.mon.test_hits(self.cfg.load())
        self.assertEqual(hits[0].item.id, "m2")
        self.assertEqual(hits[0].item.url, "https://jp.mercari.com/item/m2")
        self.assertTrue(hits[0].watch_name.startswith("测试"))
        self.assertIn("最近一条上新", where)

    def test_test_push_searches_when_nothing_found_yet(self):
        self.cfg.update(lambda c: c["watches"][0].update({"price_max": 2000}))
        self.src.pages.append([item(7, sold=True), item(8, price=9000), item(9, price=1500)])
        hits, where = self.mon.test_hits(self.cfg.load())
        self.assertEqual(hits[0].item.id, "m9")      # 跳过已售、跳过不符合价格的
        self.assertIn("假煤炉", where)
        self.assertEqual(self.store.feed(), [])      # 测试不会写进上新记录

    def test_test_push_falls_back_to_sample(self):
        from goodsmare import net
        self.src.pages.append(net.FetchError("连不上"))
        hits, where = self.mon.test_hits(self.cfg.load())
        self.assertIn("示例", hits[0].item.title)
        self.assertIn("示例", where)

    def test_passes_normalizes_width_and_case(self):
        w = {"price_min": 0, "price_max": 0, "must": ["ABC", "バッジ"], "exclude": []}
        self.assertTrue(passes(w, item(1, title="ａｂｃ ｶﾝﾊﾞｯｼﾞ")))

    def test_must_with_alternatives(self):
        # 真实用过的关注：标题里有写 fishmans 的，也有写 フィッシュマンズ 的
        w = {"price_min": 0, "price_max": 0, "must": ["フィッシュマンズ|fishmans"], "exclude": []}
        self.assertTrue(passes(w, item(1, title="Fishmans ナイトクルージング")))
        self.assertTrue(passes(w, item(2, title="フィッシュマンズ 空中キャンプ")))
        self.assertFalse(passes(w, item(3, title="宇多田ヒカル ステッカー タワーレコード")))
        # 逗号还是"都要有"，竖线的全角写法也认
        w["must"] = ["五条｜五條", "缶バッジ"]
        self.assertTrue(passes(w, item(4, title="五條悟 缶バッジ")))
        self.assertFalse(passes(w, item(5, title="五条悟 アクスタ")))
        w["must"] = ["|"]
        self.assertTrue(passes(w, item(6)), "空的备选不能把所有东西都挡掉")

    def test_exclude_with_alternatives(self):
        w = {"price_min": 0, "price_max": 0, "must": [], "exclude": ["まとめ|セット", "|"]}
        self.assertFalse(passes(w, item(1, title="缶バッジ まとめ売り")))
        self.assertFalse(passes(w, item(2, title="缶バッジ 5種セット")))
        self.assertTrue(passes(w, item(3, title="缶バッジ 単品")))
        from goodsmare.monitor import query_of
        w.update(keyword="x", exclude=["まとめ|セット", "空箱"])
        self.assertEqual(query_of(w).exclude, ["まとめ", "セット", "空箱"])

    def test_parser_rev_rebaselines_without_false_drops(self):
        # 旧版骏河屋把运费说明里的 5,000 当成了价格；修好之后价格"变低"不能报降价
        self.cfg.update(lambda c: c["watches"][0].update({"price_drop": True}))
        self.cycle([item(1, price=5000), item(2, price=5000)])
        self.assertEqual(self.cycle([item(1, price=5000), item(2, price=5000)]), [])
        self.src.rev = 2
        self.assertEqual(self.cycle([item(3, price=900), item(1, price=1790), item(2, price=1020)]), [])
        self.assertIn("首次扫描", self.status()["message"])
        hits = self.cycle([item(4), item(3, price=900), item(1, price=1500), item(2, price=1020)])
        self.assertEqual(sorted((h.item.id, h.kind) for h in hits), [("m1", "drop"), ("m4", "new")])


if __name__ == "__main__":
    unittest.main()
