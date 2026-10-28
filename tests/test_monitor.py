import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from goodsmare.config import ConfigStore
from goodsmare.monitor import Monitor, passes
from goodsmare.notify import Hit
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


    def test_unreachable_skips_the_rest_of_the_site(self):
        from goodsmare import net
        self.cfg.update(lambda c: c["watches"].append({"id": "w2", "keyword": "夏油傑", "sources": ["mercari"]}))
        self.src.pages.append(net.Unreachable("连不上 api.mercari.jp（超时）", "api.mercari.jp"))
        with mock.patch.object(net, "proxy_info", return_value={"mode": "direct", "proxy": ""}), \
                mock.patch.object(net, "find_local_proxy", return_value="http://127.0.0.1:7890") as find:
            self.mon.run_cycle()
        self.assertEqual(len(self.src.queries), 1)   # 第二个关注没有再去等一次超时
        st = self.store.statuses()
        self.assertEqual({st["w1"]["mercari"]["kind"], st["w2"]["mercari"]["kind"]}, {"net"})
        self.assertIn("超时", st["w2"]["mercari"]["message"])
        find.assert_called_once_with("api.mercari.jp")
        self.assertEqual(self.mon.proxy_hint, "http://127.0.0.1:7890")
        # 走着代理还连不上，就不用找本机代理了
        self.src.pages.append(net.Unreachable("连不上 api.mercari.jp（超时）", "api.mercari.jp"))
        with mock.patch.object(net, "proxy_info", return_value={"mode": "system", "proxy": "http://127.0.0.1:7890"}), \
                mock.patch.object(net, "find_local_proxy") as find:
            self.mon.run_cycle()
        find.assert_not_called()
        self.assertEqual(self.mon.proxy_hint, "")

    def test_cny_prices_are_compared_in_yen(self):
        w = {"price_min": 0, "price_max": 3000, "must": [], "exclude": []}

        def cny(p):
            return Item(source="xianyu", id="1", title="黑胶", price=p, url="")
        self.assertTrue(passes(w, cny(100), 0.05))     # 100 元 ≈ 2000 日元
        self.assertFalse(passes(w, cny(200), 0.05))    # 200 元 ≈ 4000 日元
        self.assertTrue(passes(w, cny(200), 0))        # 不知道汇率就不比价格
        self.assertFalse(passes(w, item(1, price=3500), 0.05))

    def test_slow_sites_are_not_searched_every_round(self):
        self.src.min_interval = 300
        self.cycle([item(1)])
        self.mon.run_cycle()                 # 5 分钟内不再搜
        self.assertEqual(len(self.src.queries), 1)
        self.mon.rescan_source("mercari")    # 比如闲鱼刚登录好
        self.cycle([item(1)])
        self.assertEqual(len(self.src.queries), 2)

    def test_status_kind(self):
        from goodsmare import net
        self.cycle([])
        self.assertEqual(self.status()["kind"], "parse")
        self.src.pages.append(net.Blocked("煤炉 拦下了请求（HTTP 403）"))
        self.mon.run_cycle()
        self.assertEqual(self.status()["kind"], "blocked")
        self.cycle([item(1)])
        self.assertEqual((self.status()["ok"], self.status()["kind"]), (True, ""))

    def test_old_status_table_gets_kind(self):
        import sqlite3
        path = Path(self.tmp.name) / "old.db"
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE status (watch_id TEXT, source TEXT, checked_at INTEGER, ok INTEGER, count INTEGER, "
                    "message TEXT, PRIMARY KEY (watch_id, source))")
        con.execute("INSERT INTO status VALUES ('w1', 'mercari', 1, 0, 0, '连不上 api.mercari.jp：timed out')")
        con.commit()
        con.close()
        old = Store(path)
        try:
            self.assertEqual(old.statuses()["w1"]["mercari"]["kind"], "")
            old.set_status("w1", "mercari", False, 0, "连不上 api.mercari.jp（超时）", "net")
            self.assertEqual(old.statuses()["w1"]["mercari"]["kind"], "net")
        finally:
            old.db.close()

    def test_watch_stats(self):
        self.cycle([item(1)])
        self.cycle([item(2), item(1)])
        self.cycle([item(3), item(2), item(1)])
        self.assertEqual(self.store.watch_stats()["w1"]["recent"], 2)
        self.store.block([("mercari", "m3", "w1")])
        self.assertEqual(self.store.watch_stats()["w1"]["recent"], 1)


    def test_catch_up_after_a_long_stop(self):
        """程序停了好几个小时再开：这段时间冒出来的只推最新几件，其余的记进上新、不推。"""
        from goodsmare import monitor
        self.cfg.update(lambda c: c.update({"channels": [{"type": "webhook", "url": "http://x/hook"}]}))
        self.cycle([item(1)])
        self.store.db.execute("UPDATE status SET checked_at = checked_at - ?", (monitor.CATCHUP_GAP + 60,))
        self.store.db.commit()
        page = [Item(source="mercari", id=f"m{i}", title=f"五条悟 缶バッジ {i}", price=1000,
                     url=f"https://jp.mercari.com/item/m{i}", created=1000 + i) for i in range(100, 112)]
        with mock.patch("goodsmare.notify.send_all", return_value=[]) as send:
            pushed = self.cycle(page + [item(1)])
        self.assertEqual([h.item.id for h in pushed], [f"m{i}" for i in range(111, 106, -1)], "最新的 5 件")
        self.assertEqual(len(send.call_args[0][1]), monitor.CATCHUP_PUSH)
        self.assertEqual(len(self.store.feed(limit=100)), 12, "其余的也在上新页里")
        # 之后照常
        self.assertEqual(len(self.cycle([item(200)] + page)), 1)

    def test_catch_up_batches_share_one_budget(self):
        """常驻时补查到的货分好几批送来：几批加起来也只推 5 件。"""
        def quiet(i):
            return Hit(Item(source="mercari", id=f"q{i}", title=f"补 {i}", price=1000, url=f"u{i}", created=i), "w1", "x",
                       quiet=True)
        with mock.patch("goodsmare.notify.send_all", return_value=[]):
            self.cfg.update(lambda c: c.update({"channels": [{"type": "webhook", "url": "http://x/hook"}]}))
            first = self.mon.deliver([quiet(i) for i in range(3)])
            second = self.mon.deliver([quiet(i) for i in range(3, 10)])
        self.assertEqual((len(first), len(second)), (3, 2))
        self.assertEqual(len(self.store.feed(limit=100)), 10)

    def test_small_price_drops_are_ignored(self):
        self.cfg.update(lambda c: c["watches"][0].update({"price_drop": True}))
        self.cycle([item(1, price=3000)])
        self.assertEqual(self.cycle([item(1, price=2850)]), [], "便宜 5% 不提醒")
        hits = self.cycle([item(1, price=2500)])
        self.assertEqual((hits[0].kind, hits[0].old_price), ("drop", 2850))

    def test_relisted_items_are_not_pushed_again(self):
        """卖家删了重新上架：编号变了，标题和价格一样，7 天内不再推。"""
        self.cycle([item(1)])
        self.assertEqual(len(self.cycle([item(2, title="ミドリ CD 帯付き"), item(1)])), 1)
        self.assertEqual(self.cycle([item(3, title="ミドリ CD 帯付き"), item(2), item(1)]), [])
        self.assertEqual(len(self.cycle([item(4, title="ミドリ CD 帯付き", price=900), item(3)])), 1, "价格变了照推")

    def test_live_mode_checks_each_site_on_its_own(self):
        """常驻：每个网站一个线程轮着查，查到新货等几秒合成一条推送。"""
        from goodsmare import monitor
        self.cfg.update(lambda c: c.update({"interval": 10, "request_gap": 0.5}))
        self.src.pages = [[item(1)], [item(2), item(1)]] + [[item(2), item(1)]] * 50
        sent = []
        with mock.patch.object(monitor, "PUSH_QUIET", 0.1), mock.patch.object(monitor, "PUSH_MAX_WAIT", 0.5), \
                mock.patch("goodsmare.notify.send_all", side_effect=lambda ch, hits, ctx: sent.append(hits) or []):
            self.cfg.update(lambda c: c.update({"channels": [{"type": "webhook", "url": "http://x/hook"}]}))
            t = threading.Thread(target=self.mon.run_forever, daemon=True)
            t.start()
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not self.store.statuses().get("w1"):
                    time.sleep(0.05)
                self.assertIn("首次扫描", self.status()["message"])
                self.assertTrue(self.mon.live)
                self.mon.trigger()   # 「立即扫描」：不等 10 秒的间隔
                deadline = time.time() + 5
                while time.time() < deadline and not sent:
                    time.sleep(0.05)
            finally:
                self.mon.stop()
                t.join(5)
        self.assertEqual([[h.item.id for h in batch] for batch in sent], [["m2"]])
        self.assertEqual(self.mon.period, 10)

    def test_a_second_pause_cannot_enter_or_release_the_first(self):
        entered = threading.Event()

        def scan():
            with self.mon._gate:
                entered.set()

        t = threading.Thread(target=scan, daemon=True)
        try:
            with self.mon.paused(1):
                with self.assertRaises(TimeoutError):
                    with self.mon.paused(0):
                        pass
                t.start()
                self.assertFalse(entered.wait(0.1), "第二个暂停超时后，第一个暂停仍然有效")
        finally:
            if t.ident is not None:
                t.join(1)
        self.assertTrue(entered.is_set())

    def test_pending_push_waits_until_migration_finishes(self):
        from goodsmare import monitor

        delivered = threading.Event()
        t = threading.Thread(target=self.mon._push_loop, daemon=True)
        with mock.patch.object(self.mon, "deliver", side_effect=lambda hits: delivered.set()), \
                mock.patch.object(monitor, "PUSH_QUIET", 0):
            try:
                with self.mon.paused(1):
                    self.mon._enqueue([Hit(item(2), "w1", "五条悟")])
                    t.start()
                    self.assertFalse(delivered.wait(0.1), "迁移暂停期间不能写上新记录或发推送")
                self.assertTrue(delivered.wait(1))
            finally:
                self.mon.stop()
                if t.ident is not None:
                    t.join(2)


if __name__ == "__main__":
    unittest.main()
