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
        self.assertEqual([s["key"] for s in state["sources"]], ALL_SOURCES)
        self.assertEqual({s["key"]: s["currency"] for s in state["sources"]}["xianyu"], "CNY")
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

    def test_new_channel_forms_are_exposed_with_setup_guides(self):
        from tests.test_notify_extra import NEW_CHANNELS

        _, state = self.req("GET", "/api/state")
        for kind in NEW_CHANNELS:
            with self.subTest(kind=kind):
                form = state["channel_types"][kind]
                self.assertTrue(form["fields"])
                self.assertTrue(form["guide"]["url"].startswith("https://"))
                self.assertTrue(form["guide"]["text"])

    def test_new_channels_can_be_saved_and_tested_from_the_web(self):
        from tests.test_notify_extra import NEW_CHANNELS, response

        self.add_feed("m1", "测试商品")

        def post(url, **kwargs):
            if "wxpusher" in url:
                return response({"code": 1000})
            if "xtuis" in url:
                return response({"msg_id": "wx_test"})
            if "iyuu" in url:
                return response({"errcode": 0})
            if "push.i-i.me" in url:
                return response("success")
            if "discord" in url:
                return response({"id": "1"})
            if "pushover" in url:
                return response({"status": 1})
            self.fail(f"unexpected URL: {url}")

        with mock.patch("goodsmare.net.post", side_effect=post) as send:
            for kind, fields in NEW_CHANNELS.items():
                with self.subTest(kind=kind):
                    status, channel = self.req("POST", "/api/channel", {"type": kind, **fields})
                    self.assertEqual(status, 200, channel)
                    status, result = self.req("POST", "/api/channel/test", {"id": channel["id"]})
                    self.assertEqual(status, 200, result)
                    self.assertIn("已发送", result["message"])
            self.assertEqual(send.call_count, len(NEW_CHANNELS))
        self.assertEqual({c["type"] for c in self.cfg.load()["channels"]}, set(NEW_CHANNELS))

    def test_bad_wxpusher_targets_are_rejected_when_saving(self):
        for fields in ({"token": "AT_test"}, {"token": "AT_test", "topic_ids": "not-a-number"}):
            with self.subTest(fields=fields):
                status, result = self.req("POST", "/api/channel", {"type": "wxpusher", **fields})
                self.assertEqual(status, 400, result)
                self.assertIn("WxPusher", result["error"])
        self.assertEqual(self.cfg.load()["channels"], [])

    def test_settings(self):
        self.req("POST", "/api/settings", {"interval": 5, "proxy": " 127.0.0.1:7890 ", "evil": 1, "buy": "doorzo"})
        cfg = self.cfg.load()
        self.assertEqual(cfg["interval"], 10)     # 有下限
        self.assertEqual(cfg["buy"], "doorzo")
        self.req("POST", "/api/settings", {"buy": "nope"})
        self.assertEqual(self.cfg.load()["buy"], "")
        self.assertEqual(cfg["proxy"], "127.0.0.1:7890")
        self.assertNotIn("evil", cfg)

    def test_message_schema_and_static_assets(self):
        _, state = self.req("GET", "/api/state")
        self.assertEqual(len(state["message_layout"]["blocks"]), 24)
        self.assertEqual(set(state["message_layout"]["capabilities"]), set(state["channel_types"]))
        for filename in ("message-designer.js", "message-designer.css"):
            self.assertEqual(self.req("GET", "/static/" + filename)[0], 200)
        self.assertEqual(self.req("GET", "/static/../config.py")[0], 404)
        self.cfg.update(lambda c: c["web"].update({"token":"secret"}))
        self.assertEqual(self.req("GET", "/static/message-designer.js")[0], 401)

    def test_message_save_preserves_channel_credentials_and_other_config(self):
        _, ch = self.req("POST", "/api/channel", {"type":"wxpusher","token":"SPT_secret","name":"我的渠道"})
        original = self.cfg.load()
        status, result = self.req("POST", "/api/message/settings", {"message":{"blocks":["title","price"]}})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.cfg.load()["channels"], original["channels"])
        status, result = self.req("POST", "/api/message/settings", {"channel_id":ch["id"],"message":{"blocks":["note"],"note":"自定义"}})
        self.assertEqual(status, 200, result)
        saved = self.cfg.load()
        self.assertEqual(saved["channels"][0]["token"], "SPT_secret")
        self.assertEqual(saved["channels"][0]["message"]["blocks"], ["note"])
        self.assertEqual(saved["watches"], original["watches"])
        self.assertEqual(saved["message"]["blocks"], ["title","price"])
        self.assertNotIn("SPT_secret", json.dumps(result))
        # 修改接收渠道的表单不带布局字段，也必须保留独立布局。
        self.req("POST", "/api/channel", {**ch,"name":"改名"})
        self.assertEqual(self.cfg.load()["channels"][0]["message"], saved["channels"][0]["message"])
        self.req("POST", "/api/message/settings", {"channel_id":ch["id"],"inherit":True})
        self.assertNotIn("message", self.cfg.load()["channels"][0])

    def test_message_rejects_empty_enabled_layout_and_missing_channel(self):
        before = self.cfg.load()
        for body in ({"message":{"blocks":[]}}, {"message":"bad"},
                     {"channel_id":"missing","message":{"blocks":["title"]}}):
            status, _ = self.req("POST", "/api/message/settings", body)
            self.assertEqual(status, 400, body)
            self.assertEqual(self.cfg.load(), before)
        self.assertEqual(self.req("POST", "/api/message/settings", {"message":{"enabled":False,"blocks":[]}})[0],200)

    def test_message_preview_has_no_network_sends_config_or_page_writes(self):
        self.add_feed("m123", "真实商品 <script>oops</script>")
        before = self.cfg.load()
        with mock.patch("goodsmare.net.post", side_effect=AssertionError("preview must not send")), \
             mock.patch("goodsmare.net.get", side_effect=AssertionError("preview must not fetch")), \
             mock.patch.object(self.mon, "ctx", side_effect=AssertionError("preview must not create pages")), \
             mock.patch.object(self.store, "add_page", side_effect=AssertionError("preview must not write pages")):
            status, data = self.req("POST", "/api/message/preview", {"message":{"blocks":["title","price","copy_original"]}})
            self.assertEqual(status,200,data)
            self.assertIn("真实商品",data["html"])
            self.assertNotIn("<script>",data["html"])
            self.assertIn("<copy ",data["html"])
            self.assertEqual(data["sample_label"],"最近发现的商品")
            for sample in ("example","drop","xianyu"):
                status, data = self.req("POST", "/api/message/preview", {"sample":sample,"type":"feishu","message":{"blocks":["price","cny","drop","buy_button"]}})
                self.assertEqual(status,200,data)
                if sample == "drop": self.assertIn("25%",data["text"])
                if sample == "xianyu":
                    self.assertIn("88元",data["text"])
                    self.assertNotIn("去任意门",data["html"])
        self.assertEqual(self.cfg.load(),before)
        self.assertEqual(len(self.store.feed()),1)

    def test_message_preview_channel_override_and_inherit(self):
        _, ch = self.req("POST", "/api/channel", {"type":"wxpusher","token":"SPT_secret","message":{"blocks":["note"],"note":"渠道独立"}})
        self.req("POST", "/api/message/settings", {"message":{"blocks":["note"],"note":"全局内容"}})
        _, data = self.req("POST", "/api/message/preview", {"channel_id":ch["id"]})
        self.assertIn("渠道独立",data["html"])
        self.assertNotIn("全局内容",data["html"])
        _, data = self.req("POST", "/api/message/preview", {"channel_id":ch["id"],"inherit":True})
        self.assertIn("全局内容",data["html"])
        self.assertNotIn("SPT_secret",json.dumps(data))
        self.assertEqual(self.req("POST", "/api/message/preview", {"channel_id":"missing"})[0],400)

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

    def test_migrate_activates_the_imported_xianyu_account(self):
        from goodsmare.sources import xianyu

        old = Path(self.tmp.name) / "old" / "data"
        old.mkdir(parents=True)
        st = Store(old / "goodsmare.db")
        try:
            acc = xianyu.Account()
            acc.attach(st)
            acc.use_cookie_text("unb=3300; tracknick=imported")
        finally:
            st.db.close()
        self.assertFalse(xianyu.account.logged_in())
        self.mon._wake.clear()
        status, body = self.req("POST", "/api/migrate", {"path": str(old.parent), "apply": True})
        self.assertEqual(status, 200, body)
        _, state = self.req("GET", "/api/state")
        self.assertTrue(state["xianyu"]["logged_in"], "迁移完成后不用重启就能用闲鱼")
        self.assertEqual(state["xianyu"]["nick"], "imported")
        self.assertTrue(next(s for s in state["sources"] if s["key"] == "xianyu")["ready"])
        self.assertTrue(self.mon._wake.is_set(), "迁移完成后马上唤醒扫描")

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


    def test_problems_are_grouped(self):
        self.cfg.update(lambda c: c.update({"watches": [
            {"id": "w1", "name": "甲", "keyword": "ミドリ cd", "sources": ["mercari", "surugaya", "yahoo_flea"]},
            {"id": "w2", "name": "乙", "keyword": "b", "sources": ["mercari", "surugaya"]},
            {"id": "w3", "name": "丙", "keyword": "c", "sources": ["mercari"], "enabled": False}]}))
        for wid in ("w1", "w2", "w3"):
            self.store.set_status(wid, "mercari", False, 0, "连不上 api.mercari.jp（超时）", "net")
        self.store.set_status("w1", "surugaya", False, 0, "骏河屋 拦下了请求（HTTP 403）", "blocked")
        self.store.set_status("w2", "surugaya", True, 10, "本次 10 件，新的 0 件")
        # 旧版记下的状态没有 kind，看字猜
        self.store.set_status("w1", "yahoo_flea", False, 0, "连不上 paypayfleamarket.yahoo.co.jp：timed out")
        _, state = self.req("GET", "/api/state")
        probs = {(p["source"], p["kind"]): p for p in state["problems"]}
        self.assertEqual(set(probs), {("mercari", "net"), ("surugaya", "blocked"), ("yahoo_flea", "net")})
        self.assertEqual(probs[("mercari", "net")]["watches"], ["甲", "乙"])   # 暂停的关注不算
        self.assertTrue(probs[("mercari", "net")]["all"])
        self.assertEqual((probs[("surugaya", "blocked")]["watches"], probs[("surugaya", "blocked")]["all"]), (["甲"], False))
        self.assertIn("keyword=%E3%83%9F%E3%83%89%E3%83%AA", state["links"]["w1"]["mercari"])
        self.assertEqual(set(state["links"]["w1"]), {"mercari", "surugaya", "yahoo_flea"})
        self.assertIn(state["net"]["mode"], ("app", "system", "direct"))
        self.assertFalse(state["xianyu"]["logged_in"])

    def test_use_proxy(self):
        self.mon.proxy_hint = "http://127.0.0.1:7890"
        try:
            status, _ = self.req("POST", "/api/settings", {"proxy": "http://127.0.0.1:7890"})
            self.assertEqual(status, 200)
            self.assertEqual(self.cfg.load()["proxy"], "http://127.0.0.1:7890")
            self.assertEqual(self.mon.proxy_hint, "")
            _, state = self.req("GET", "/api/state")
            self.assertEqual(state["net"], {"mode": "app", "proxy": "http://127.0.0.1:7890", "local": ""})
        finally:
            self.req("POST", "/api/settings", {"proxy": ""})

    def test_xianyu_login_flow(self):
        from goodsmare.sources import SOURCES
        self.cfg.update(lambda c: c.update({"watches": [
            {"id": "w1", "keyword": "GYBE", "sources": ["mercari"]},
            {"id": "w2", "keyword": "黑胶", "sources": ["mercari", "xianyu"]}]}))
        fake = mock.Mock()
        fake.qr_start.return_value = "https://passport.goofish.com/qrcodeCheck.htm?lgToken=x"
        fake.qr_poll.side_effect = [{"status": "new"}, {"status": "done"}]
        fake.state.return_value = {"logged_in": True, "nick": "tb88", "at": 1, "scanning": False}
        found = [Item(source="xianyu", id="1", title="黑胶", price=380, url="https://www.goofish.com/item?id=1")]
        with mock.patch("goodsmare.web.xianyu_account", fake), \
                mock.patch.object(SOURCES["xianyu"], "search", return_value=found) as search:
            _, d = self.req("POST", "/api/xianyu/qr", {})
            self.assertIn("qrcodeCheck", d["code"])
            _, d = self.req("POST", "/api/xianyu/poll", {})
            self.assertEqual(d["status"], "new")
            self.mon._wake.clear()
            _, d = self.req("POST", "/api/xianyu/poll", {})
        self.assertEqual((d["status"], d["check_ok"], d["nick"]), ("done", True, "tb88"))
        self.assertEqual(search.call_args[0][0].keyword, "黑胶")   # 拿搜闲鱼的那个关注的关键词试搜
        self.assertTrue(self.mon._wake.is_set())                     # 马上扫一轮
        _, d = self.req("POST", "/api/xianyu/add", {})
        self.assertEqual(d["added"], 1)
        self.assertEqual([w["sources"] for w in self.cfg.load()["watches"]], [["mercari", "xianyu"]] * 2)

    def test_xianyu_cookie_needs_login_info(self):
        status, d = self.req("POST", "/api/xianyu/cookie", {"cookie": "cna=1; cookie2=2"})
        self.assertEqual(status, 400)
        self.assertIn("unb", d["error"])


if __name__ == "__main__":
    unittest.main()
