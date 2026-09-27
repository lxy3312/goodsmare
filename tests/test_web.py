import http.client
import json
import tempfile
import unittest
from pathlib import Path

from goodsmare.config import ConfigStore
from goodsmare.monitor import Monitor
from goodsmare.store import Store
from goodsmare.web import App, serve


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = ConfigStore(d / "config.json")
        self.cfg.update(lambda c: c.update({"auto_rate": False}))
        self.store = Store(d / "goodsmare.db")
        self.mon = Monitor(self.cfg, self.store, echo=False)
        self.server = serve(App(self.cfg, self.store, self.mon), "127.0.0.1", 0)
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.db.close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def test_page_and_state(self):
        status, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("goodsmare".encode(), body)
        status, state = self.req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(state["sources"]), 6)
        self.assertIn("serverchan", state["channel_types"])

    def test_watch_crud(self):
        status, w = self.req("POST", "/api/watch", {"keyword": " 五条悟 缶バッジ ", "must": "五条，缶",
                                                    "price_max": "3000", "sources": ["mercari", "nope"]})
        self.assertEqual(status, 200)
        self.assertEqual(w["keyword"], "五条悟 缶バッジ")
        self.assertEqual(w["must"], ["五条", "缶"])
        self.assertEqual(w["price_max"], 3000)
        self.assertEqual(w["sources"], ["mercari"])
        self.assertEqual(self.cfg.load()["watches"][0]["id"], w["id"])
        w["enabled"] = False
        self.req("POST", "/api/watch", w)
        self.assertFalse(self.cfg.load()["watches"][0]["enabled"])
        self.assertEqual(len(self.cfg.load()["watches"]), 1)
        self.req("POST", "/api/watch/delete", {"id": w["id"]})
        self.assertEqual(self.cfg.load()["watches"], [])

    def test_validation(self):
        status, body = self.req("POST", "/api/watch", {"keyword": ""})
        self.assertEqual(status, 400)
        self.assertIn("关键词", body["error"])
        status, body = self.req("POST", "/api/channel", {"type": "bark"})
        self.assertEqual(status, 400)

    def test_settings(self):
        self.req("POST", "/api/settings", {"interval": 5, "proxy": " 127.0.0.1:7890 ", "evil": 1})
        cfg = self.cfg.load()
        self.assertEqual(cfg["interval"], 30)     # 有下限
        self.assertEqual(cfg["proxy"], "127.0.0.1:7890")
        self.assertNotIn("evil", cfg)

    def test_rejects_foreign_host_and_non_json(self):
        status, _ = self.req("GET", "/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/scan", "{}", {"Content-Type": "text/plain"})
        self.assertEqual(conn.getresponse().status, 415)
        conn.close()

    def test_token(self):
        self.cfg.update(lambda c: c["web"].update({"token": "s3cret"}))
        status, _ = self.req("GET", "/api/state")
        self.assertEqual(status, 401)
        status, _ = self.req("GET", "/api/state", headers={"Cookie": "goodsmare_token=s3cret"})
        self.assertEqual(status, 200)
        status, _ = self.req("GET", "/?token=s3cret")
        self.assertEqual(status, 302)


if __name__ == "__main__":
    unittest.main()
