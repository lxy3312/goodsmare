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


if __name__ == "__main__":
    unittest.main()
