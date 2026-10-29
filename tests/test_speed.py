import collections
import contextlib
import tempfile
import threading
import time
import unittest
from email.utils import formatdate
from pathlib import Path
from unittest import mock

from goodsmare import net, notify
from goodsmare.config import ConfigStore, normalize, normalize_watch
from goodsmare.dispatch import Dispatcher
from goodsmare.monitor import CoolingDown, Monitor, query_of, watch_periods
from goodsmare.sources import SOURCES, NoResults
from goodsmare.store import Store
from goodsmare.web import App
from tests.test_monitor import FakeSource, item


class SpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = ConfigStore(root / "config.json")
        self.cfg.save({"auto_rate": False, "interval": 20, "request_gap": 3, "watches": [
            {"id": wid, "keyword": wid, "sources": ["mercari"], "priority": priority}
            for wid, priority in [("h", "high"), ("n", "normal"), ("l", "low")]]})
        self.store = Store(root / "test.db")
        self.src = FakeSource()
        self.patch = mock.patch.dict(SOURCES, {"mercari": self.src})
        self.patch.start()
        self.mon = Monitor(self.cfg, self.store, echo=False)
        self.mon._polite_wait = mock.Mock()

    def tearDown(self):
        self.mon.stop()
        self.mon._dispatcher.close()
        self.patch.stop()
        self.store.db.close()
        self.tmp.cleanup()

    def test_priorities_redistribute_but_do_not_raise_request_budget(self):
        cfg = self.cfg.load()
        periods = watch_periods(cfg, "mercari")
        self.assertLess(periods["h"], periods["n"])
        self.assertLess(periods["n"], periods["l"])
        self.assertLessEqual(sum(1 / p for p in periods.values()), 3 / cfg["interval"] + 1e-9)
        self.src.min_interval = 300
        self.assertGreaterEqual(min(watch_periods(cfg, "mercari").values()), 300)

    def test_busy_site_still_checks_low_priority_watches(self):
        cfg = self.cfg.load()
        counts = collections.Counter()
        now = 10000
        for _ in range(60):
            watch, wait = self.mon._pick_watch("mercari", cfg["watches"], cfg, now)
            now += wait
            counts[watch["id"]] += 1
            self.mon._last_scan[(watch["id"], "mercari")] = now
            now += 15  # 模拟慢请求导致持续拥堵
        self.assertGreater(counts["h"], counts["n"])
        self.assertGreater(counts["n"], counts["l"])
        self.assertGreater(counts["l"], 5)

    def test_manual_scan_keeps_minimum_period_and_cooldown(self):
        self.src.min_interval = 300
        now = time.time()
        for wid in ("h", "n", "l"):
            self.mon._last_scan[(wid, "mercari")] = now
        self.mon._backoff("mercari", net.Blocked("slow down", retry_after=1200))
        until = self.mon._backoff_until["mercari"]
        self.mon.trigger("h")
        self.assertEqual(self.mon._requested, {("h", "mercari")})
        self.assertEqual(self.mon._last_scan[("h", "mercari")], now)
        self.assertEqual(self.mon._backoff_until["mercari"], until)
        cfg = self.cfg.load()
        _, wait = self.mon._pick_watch("mercari", cfg["watches"], cfg, now)
        self.assertGreaterEqual(wait, 300)
        self.mon.run_cycle()
        self.assertEqual(self.src.queries, [])

    def test_restart_and_relogin_cannot_erase_rate_limit(self):
        self.mon._backoff("mercari", net.Blocked("too fast", retry_after=1200))
        restarted = Monitor(self.cfg, self.store, echo=False)
        restarted.rescan_source("mercari")
        with self.assertRaises(CoolingDown):
            restarted.search("mercari", query_of(self.cfg.load()["watches"][0]), self.cfg.load())
        self.assertEqual(self.src.queries, [])
        self.assertGreater(restarted._backoff_until["mercari"], time.time() + 1100)

    def test_login_failure_pauses_until_login_is_restored(self):
        class LoginError(net.FetchError):
            kind = "login"
        self.src.pages = [LoginError("请登录"), NoResults()]
        self.mon.run_cycle()
        self.mon.trigger()
        self.mon.run_cycle()
        self.assertEqual(len(self.src.queries), 1)
        self.assertTrue(self.mon.timing_snapshot()["cooldowns"]["mercari"]["login_required"])
        self.mon.rescan_source("mercari")
        cfg = self.cfg.load()
        self.mon.search("mercari", query_of(cfg["watches"][0]), cfg)
        self.assertEqual(len(self.src.queries), 2)
        self.assertNotIn("mercari", self.mon.timing_snapshot()["cooldowns"])

    def test_editing_one_watch_does_not_force_every_watch(self):
        app = App(self.cfg, self.store, self.mon)
        watch = self.cfg.load()["watches"][0]
        self.mon._backoff("mercari", net.Blocked("limited"))
        until = self.mon._backoff_until["mercari"]
        app.save_watch({**watch, "keyword": "changed"})
        self.assertEqual(self.mon._requested, {("h", "mercari")})
        self.assertEqual(self.mon._backoff_until["mercari"], until)
        self.mon._requested.clear()
        app.save_watch({**self.cfg.load()["watches"][0], "priority": "normal", "name": "new name"})
        self.assertEqual(self.mon._requested, set(), "名称和优先级变化不额外扫描")

    def test_preview_and_test_search_share_cooldown(self):
        app = App(self.cfg, self.store, self.mon)
        cfg = self.cfg.load()
        self.src.pages = [NoResults()]
        app.preview(cfg["watches"][0])
        self.mon._polite_wait.assert_called_once_with("mercari", 3)
        self.assertIn("mercari", self.mon._last_request)
        self.mon._backoff("mercari", net.Blocked("limited"))
        with self.assertRaises(CoolingDown):
            app.preview(cfg["watches"][0])
        hits, _ = self.mon.test_hits(cfg)
        self.assertIn("示例", hits[0].item.title)
        self.assertEqual(len(self.src.queries), 1)

    def test_instant_and_priority_push_do_not_wait_for_batch_window(self):
        cfg = self.cfg.load()
        urgent = notify.Hit(item(1), "h", "重点")
        normal = notify.Hit(item(2), "n", "普通")
        cfg["push_mode"] = "priority"
        with mock.patch("goodsmare.monitor.time.monotonic", return_value=100):
            self.mon._enqueue([normal, urgent])
            self.assertEqual(self.mon._take_pending(cfg), [urgent])
            self.assertEqual(self.mon._take_pending(cfg), [])
        with mock.patch("goodsmare.monitor.time.monotonic", return_value=103):
            self.assertEqual(self.mon._take_pending(cfg), [normal])
        cfg["push_mode"] = "instant"
        self.mon._enqueue([normal])
        self.assertEqual(self.mon._take_pending(cfg), [normal])

    def test_continuous_arrivals_cannot_postpone_batch_forever(self):
        cfg = self.cfg.load()
        cfg["push_mode"] = "batch"
        first = notify.Hit(item(1), "n", "普通")
        second = notify.Hit(item(2), "n", "普通")
        with mock.patch("goodsmare.monitor.time.monotonic", return_value=100):
            self.mon._enqueue([first])
        with mock.patch("goodsmare.monitor.time.monotonic", return_value=111):
            self.mon._enqueue([second])
            self.assertEqual(self.mon._take_pending(cfg), [])
        with mock.patch("goodsmare.monitor.time.monotonic", return_value=112):
            self.assertEqual(self.mon._take_pending(cfg), [first, second])

    def test_first_rate_failure_is_not_retried_by_every_scan_or_push(self):
        cfg = self.cfg.load()
        cfg["auto_rate"] = True
        with mock.patch("goodsmare.net.get", side_effect=net.FetchError("offline")) as get:
            self.mon.rate(cfg)
            self.mon.rate(cfg)
            self.mon.ctx(cfg)
        get.assert_called_once()

    def test_priority_and_push_settings_are_normalized_and_roundtrip(self):
        app = App(self.cfg, self.store, self.mon)
        app.save_settings({"push_mode": "priority"})
        self.assertEqual(self.cfg.load()["push_mode"], "priority")
        self.assertEqual(normalize({"push_mode": None})["push_mode"], "instant")
        self.assertEqual(normalize_watch({"priority": []})["priority"], "normal")
        cfg = normalize({"interval": float("inf"), "request_gap": float("inf"), "jpy_to_cny": -1})
        self.assertEqual((cfg["interval"], cfg["request_gap"], cfg["jpy_to_cny"]), (60, 3, 0.048))


class DispatcherTests(unittest.TestCase):
    def test_slow_channel_does_not_delay_other_channels_later_batches(self):
        entered, release, fast_done = threading.Event(), threading.Event(), threading.Event()
        received = []
        logs = []
        dispatcher = Dispatcher(contextlib.nullcontext(), logs.append)

        def send(channels, hits, ctx):
            channel = channels[0]["id"]
            if channel == "slow":
                entered.set()
                release.wait(3)
            received.append((channel, hits[0].item.id))
            if ("fast", "m2") in received:
                fast_done.set()
            return []

        with mock.patch("goodsmare.notify.send_all", side_effect=send):
            try:
                for i in (1, 2):
                    for key in ("slow", "fast"):
                        dispatcher.submit({"id": key, "type": "webhook"}, [notify.Hit(item(i), "w", "x")],
                                          notify.Ctx(0.05), time.time())
                self.assertTrue(entered.wait(1))
                self.assertTrue(fast_done.wait(1), "慢渠道没结束时，快渠道必须能发送第二批")
                self.assertNotIn(("slow", "m1"), received)
            finally:
                release.set()
                dispatcher.close()
        self.assertEqual([i for key, i in received if key == "slow"], ["m1", "m2"])
        self.assertEqual(dispatcher.snapshot()["pending"], 0)
        self.assertEqual(dispatcher.snapshot()["samples"], 4)

    def test_failed_channel_worker_survives_and_reports_failure(self):
        logs = []
        dispatcher = Dispatcher(contextlib.nullcontext(), logs.append)
        with mock.patch("goodsmare.notify.send_all", side_effect=[[('x', 'failed')], []]):
            for i in (1, 2):
                dispatcher.submit({"id": "x", "type": "webhook"}, [notify.Hit(item(i), "w", "x")],
                                  notify.Ctx(0.05), time.time())
            dispatcher.close()
        self.assertEqual((dispatcher.snapshot()["failures"], dispatcher.snapshot()["samples"]), (1, 1))
        self.assertIn("failed", logs[0])


class RetryAfterTests(unittest.TestCase):
    def test_rate_limit_supports_seconds_and_http_dates(self):
        now = 1700000000
        for value in ("300", formatdate(now + 300, usegmt=True)):
            with self.subTest(value=value), mock.patch("goodsmare.net.time.time", return_value=now):
                response = net.Response(429, "https://example.test", {"retry-after": value}, b"slow")
                with self.assertRaises(net.Blocked) as cm:
                    response.check("test")
                self.assertEqual(cm.exception.retry_after, 300)
        self.assertEqual(net.Response(429, "", {"Retry-After": "invalid"}, b"").retry_after(), 0)

    def test_challenge_or_retry_after_503_is_not_retried_immediately(self):
        for body, headers in [(b"captcha", {}), (b"busy", {"Retry-After": "120"})]:
            with self.subTest(body=body):
                response = mock.MagicMock()
                response.status = 503
                response.headers = headers
                response.read.return_value = body
                response.geturl.return_value = "https://example.test"
                opener = mock.MagicMock()
                opener.open.return_value.__enter__.return_value = response
                with mock.patch("goodsmare.net._opener_for", return_value=opener), mock.patch("goodsmare.net.time.sleep") as sleep:
                    result = net.get("https://example.test")
                self.assertEqual(result.status, 503)
                opener.open.assert_called_once()
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
