import http.client
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from goodsmare import access, pages
from goodsmare.config import ALL_SOURCES, ConfigStore
from goodsmare.monitor import Monitor
from goodsmare.notify import Hit
from goodsmare.sources import Item
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
        self.server.close()
        self.store.db.close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        data = r.read()
        self.headers = r.headers
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
        self.assertEqual(len(state["sources"]), 7)
        self.assertIn("serverchan", state["channel_types"])

    def test_page_has_a_color_for_every_source(self):
        # 角标、状态色块都靠 --s-<网站> 上色；新加网站忘了配色，页面上就是一块灰
        _, body = self.req("GET", "/")
        html = body.decode("utf-8")
        for key in ALL_SOURCES:
            self.assertIn(f"--s-{key}:", html, f"index.html 里没有 {key} 的代表色，补一个 --s-{key}")

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

    def test_push_page_needs_no_token(self):
        self.cfg.update(lambda c: c["web"].update({"token": "s3cret"}))
        hit = Hit(Item(source="mercari", id="m1", title="五条悟 缶バッジ", price=1500,
                       url="https://jp.mercari.com/item/m1"), "w1", "吧唧")
        pid = self.store.add_page(pages.snapshot([hit], 0.05))
        # 手机从局域网地址打开，没有 cookie 也能看
        status, body = self.req("GET", f"/p/{pid}", headers={"Host": "192.168.1.5:8787"})
        self.assertEqual(status, 200)
        self.assertIn("打开原商品页", body.decode())
        self.assertIn("https://jp.mercari.com/item/m1", body.decode())
        status, body = self.req("GET", "/p/AAAAAAAAAAAAAAAA")
        self.assertEqual(status, 404)
        self.assertIn("已经不在了", body.decode())
        status, _ = self.req("GET", "/p/../api/state")
        self.assertEqual(status, 404)

    def test_login_page(self):
        self.cfg.update(lambda c: c["web"].update({"token": "s3cret"}))
        status, body = self.req("GET", "/")
        self.assertEqual(status, 401)
        self.assertIn('name="token"', body.decode())
        status, body = self.req("GET", "/?token=" + urllib.parse.quote("错的口令"))
        self.assertEqual(status, 401, "中文口令也不能把服务弄崩")
        self.assertIn("不对", body.decode())
        status, _ = self.req("GET", "/?token=s3cret")
        self.assertEqual(status, 302)
        self.assertIn("goodsmare_token=s3cret", self.headers["Set-Cookie"])

    def test_host_rules(self):
        status, _ = self.req("GET", "/api/state", headers={"Host": "192.168.1.5:8787"})
        self.assertEqual(status, 403, "没口令时只认本机地址")
        self.cfg.update(lambda c: c["web"].update({"token": "s3cret"}))
        ok = {"Host": "192.168.1.5:8787", "Cookie": "goodsmare_token=s3cret"}
        self.assertEqual(self.req("GET", "/api/state", headers=ok)[0], 200, "有口令就靠口令")
        self.assertEqual(self.req("GET", "/api/state", headers={"Host": "a.trycloudflare.com"})[0], 401)

    def test_access_switch(self):
        calls = []

        def fake_rebind(host):   # 测试里别真去监听 0.0.0.0，Windows 会弹防火墙
            calls.append(host)
            self.server.host = host
        self.server.rebind = fake_rebind
        status, _ = self.req("POST", "/api/access", {"lan": True})
        self.assertEqual(status, 200)
        web = self.cfg.load()["web"]
        self.assertEqual(web["host"], "0.0.0.0")
        self.assertEqual(calls, ["0.0.0.0"])
        self.assertTrue(web["token"], "对局域网开放就自动生成口令")
        self.assertIn(f"goodsmare_token={web['token']}", self.headers["Set-Cookie"], "这个浏览器顺手拿到口令")
        cookie = {"Cookie": f"goodsmare_token={web['token']}"}
        with mock.patch.object(access, "lan_ips", return_value=[{"ip": "192.168.1.5", "label": "局域网"}]):
            _, state = self.req("GET", "/api/state", headers=cookie)
        self.assertEqual(state["access"]["base"], "http://192.168.1.5:8787")
        self.assertTrue(state["access"]["lan"])
        status, body = self.req("POST", "/api/access", {"public_url": "ftp://x"}, headers=cookie)
        self.assertEqual(status, 400)
        self.req("POST", "/api/access", {"public_url": "100.64.1.2:8787/", "push_page": False}, headers=cookie)
        web = self.cfg.load()["web"]
        self.assertEqual(web["public_url"], "http://100.64.1.2:8787")
        self.assertFalse(web["push_page"])
        self.req("POST", "/api/access", {"lan": False}, headers=cookie)
        self.assertEqual(calls[-1], "127.0.0.1")
        self.assertEqual(self.cfg.load()["web"]["host"], "127.0.0.1")

    def test_rebind_keeps_serving(self):
        self.server.rebind("localhost")
        self.assertEqual(self.server.host, "localhost")
        self.assertEqual(self.server.server_address[1], self.port, "端口不变")
        self.assertEqual(self.req("GET", "/api/state")[0], 200)

    def test_migrate_from_web(self):
        old = Path(self.tmp.name) / "old" / "data"
        old.mkdir(parents=True)
        ConfigStore(old / "config.json").save({"watches": [{"id": "o1", "keyword": "fishmans"}]})
        st = Store(old / "ritao.db")
        st.add_feed(Hit(Item(source="mercari", id="m1", title="t", price=1, url="https://jp.mercari.com/item/m1"),
                        "o1", "fishmans"))
        st.db.close()
        status, body = self.req("POST", "/api/migrate", {"path": f' "{old.parent}" '})
        self.assertEqual(status, 200, body)
        self.assertIn("只是看看", body["report"])
        self.assertGreater(body["changes"], 0)
        self.assertEqual(self.store.feed(), [], "只看看不写")
        status, body = self.req("POST", "/api/migrate", {"path": str(old.parent), "apply": True})
        self.assertEqual(status, 200, body)
        self.assertIn("核对过了", body["report"])
        self.assertEqual([r["item_id"] for r in self.store.feed()], ["m1"])
        self.assertEqual([w["id"] for w in self.cfg.load()["watches"]], ["o1"])
        status, body = self.req("POST", "/api/migrate", {"path": str(Path(self.tmp.name) / "nope")})
        self.assertEqual(status, 400)
        self.assertIn("没找到", body["error"])

    def add_feed(self, item_id, title, watch_id="w1", name="ミドリ cd"):
        self.store.add_feed(Hit(Item(source="mercari", id=item_id, title=title, price=1000,
                                     url=f"https://jp.mercari.com/item/{item_id}"), watch_id, name))

    def test_block_items_and_words(self):
        self.cfg.update(lambda c: c.update({"watches": [
            {"id": "w1", "name": "ミドリ cd", "keyword": "ミドリ cd", "exclude": ["まとめ売り"]},
            {"id": "w2", "name": "boris", "keyword": "boris"}]}))
        self.add_feed("m1", "SEIKO 自動巻き腕時計 グリーン文字盤")
        self.add_feed("m2", "【美品】G-SHOCK グリーン カシオ")
        self.add_feed("m3", "ヤマトミチ ul ビッグポケット シャツ スレートグリーン")   # 也没有ミドリ，选了「顺带藏起来」会一起屏蔽
        self.add_feed("m4", "ミドリ ファーストパンチ CD 帯付き")                       # 真货，不能动
        self.add_feed("b1", "BORIS / Pink LP", "w2", "boris")
        status, d = self.req("POST", "/api/block", {
            "items": [{"source": "mercari", "item_id": "m1", "watch_id": "w1"},
                      {"source": "mercari", "item_id": "m2", "watch_id": "w1"}],
            "watches": {"w1": {"must_add": ["ミドリ"], "exclude_add": ["腕時計", "ｸﾞﾘｰﾝ", "まとめ売り", " "]}},
            "hide_failing": True})
        self.assertEqual(status, 200, d)
        w1 = self.cfg.load()["watches"][0]
        self.assertEqual(w1["must"], ["ミドリ"])
        self.assertEqual(w1["exclude"], ["まとめ売り", "腕時計", "ｸﾞﾘｰﾝ"], "已有的、空的不重复加")
        self.assertEqual(d["watches"], ["ミドリ cd"])
        self.assertEqual(sorted(i["item_id"] for i in d["undo"]["items"]), ["m1", "m2", "m3"])
        self.assertEqual(sorted(r["item_id"] for r in self.store.feed()), ["b1", "m4"])
        _, state = self.req("GET", "/api/state")
        self.assertEqual(state["blocked"], 3)
        # 撤销：商品放回来，关注改回原样
        status, _ = self.req("POST", "/api/unblock", d["undo"])
        self.assertEqual(status, 200)
        self.assertEqual(len(self.store.feed()), 5)
        w1 = self.cfg.load()["watches"][0]
        self.assertEqual((w1["must"], w1["exclude"]), ([], ["まとめ売り"]))

    def test_block_only_items_and_restore_all(self):
        self.add_feed("m1", "Hello Kitty ブレスレット")
        status, d = self.req("POST", "/api/block", {"items": [{"source": "mercari", "item_id": "m1", "watch_id": "gone"}]})
        self.assertEqual((status, d["blocked"], d["watches"]), (200, 1, []))
        self.assertEqual(self.store.feed(), [])
        self.req("POST", "/api/blocked/clear", {})
        self.assertEqual(len(self.store.feed()), 1)
        self.assertEqual(self.req("POST", "/api/block", {"watches": "x"})[0], 400)

    def test_port_busy_check(self):
        # 旧版还开着时先发现，别让两个程序抢同一个端口
        self.assertTrue(access.port_busy(self.port))
        self.server.close()
        self.assertFalse(access.port_busy(self.port))
        self.server = serve(App(self.cfg, self.store, self.mon), "127.0.0.1", 0)   # 给 tearDown 关

    def test_qr_svg(self):
        status, body = self.req("GET", "/api/qr.svg?text=" + urllib.parse.quote("http://192.168.1.5:8787/?token=x"))
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"<svg"))
        self.assertEqual(self.headers["Content-Type"], "image/svg+xml")

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
