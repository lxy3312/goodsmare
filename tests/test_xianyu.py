import hashlib
import json
import unittest
import urllib.parse
from unittest import mock

from goodsmare import net
from goodsmare.sources import NoResults, Query
from goodsmare.sources import xianyu
from goodsmare.store import Store


def resp(body, url="https://h5api.m.goofish.com/"):
    raw = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
    return net.Response(200, url, {"Content-Type": "application/json"}, raw)


# 照着网页版搜索接口的返回结构写的（data.resultList[].data.item.main）
SEARCH_OK = {"api": xianyu.SEARCH_API, "ret": ["SUCCESS::调用成功"], "data": {"resultList": [
    {"data": {"item": {"main": {
        "exContent": {"itemId": "870001", "title": "Godspeed You! <em>Black Emperor</em> 黑胶 &amp; CD",
                      "price": [{"text": "¥"}, {"text": "380"}], "picUrl": "//img.alicdn.com/bao/uploaded/i1/a.jpg",
                      "area": "上海", "fishTags": {"r2": {"tagList": [{"data": {"content": "包邮"}}]}}},
        "clickParam": {"args": {"price": "379.6", "publishTime": "1790000000000", "wantNum": "3"}}}}}},
    {"data": {"item": {"main": {"exContent": {"title": "猜你想搜"}, "clickParam": {"args": {}}}}}},
    {"data": {"item": {"main": {
        "exContent": {"itemId": "870002", "title": "GY!BE 签名海报", "price": [{"text": "1.5"}, {"text": "万"}],
                      "picUrl": "http://img.alicdn.com/bao/uploaded/i2/b.png"},
        "clickParam": {"args": {"tag": "freeship"}}}}}},
]}}


class XianyuParseTests(unittest.TestCase):
    def test_parse(self):
        items = xianyu.Xianyu.parse(SEARCH_OK)
        self.assertEqual([it.id for it in items], ["870001", "870002"])   # 没有商品 ID 的卡片跳过
        a, b = items
        self.assertEqual(a.title, "Godspeed You! Black Emperor 黑胶 & CD")
        self.assertEqual(a.price, 380)            # 人民币，四舍五入到元
        self.assertEqual(a.url, "https://www.goofish.com/item?id=870001")
        self.assertEqual(a.image, "https://img.alicdn.com/bao/uploaded/i1/a.jpg")
        self.assertEqual(a.extra, "上海 包邮 3人想要")
        self.assertEqual(a.created, 1790000000)
        self.assertEqual(b.price, 15000)          # 「1.5万」
        self.assertEqual(b.image, "https://img.alicdn.com/bao/uploaded/i2/b.png")
        self.assertEqual(b.extra, "包邮")

    def test_parse_falls_back_when_the_layout_changes(self):
        moved = {"ret": ["SUCCESS::调用成功"], "data": {"resultList": [
            {"cardData": {"item": {"itemId": "870003", "title": "Mogwai 黑胶", "price": "88",
                                   "picUrl": "https://img.alicdn.com/c.jpg"}}}]}}
        items = xianyu.Xianyu.parse(moved)
        self.assertEqual([(it.id, it.title, it.price) for it in items], [("870003", "Mogwai 黑胶", 88)])

    def test_empty_is_no_results(self):
        self.assertIsInstance(xianyu.Xianyu.parse({"ret": ["SUCCESS::调用成功"], "data": {"resultList": []}}), NoResults)

    def test_search_url(self):
        self.assertEqual(xianyu.Xianyu().search_url(Query("黑胶 唱片")),
                         "https://www.goofish.com/search?q=%E9%BB%91%E8%83%B6%20%E5%94%B1%E7%89%87")


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.acc = xianyu.Account()
        self.acc.attach(self.store)

    def tearDown(self):
        self.store.db.close()

    def login(self, *extra):
        self.acc.adopt([xianyu.make_cookie("unb", "2200"), xianyu.make_cookie("tracknick", "%5Cu6d4b%5Cu8bd5abc"),
                        *extra])

    def test_cookies_survive_a_restart(self):
        self.assertFalse(self.acc.logged_in())
        self.login(xianyu.make_cookie("_m_h5_tk", "tok123_1790000000000"))
        again = xianyu.Account()
        again.attach(self.store)
        self.assertTrue(again.logged_in())
        self.assertEqual(again.nick, "测试abc")
        self.assertEqual(again.token(again.jar), "tok123")
        again.logout()
        third = xianyu.Account()
        third.attach(self.store)
        self.assertFalse(third.logged_in())

    def test_cookie_text(self):
        self.acc.use_cookie_text("cookie: cna=x; unb=2200; _m_h5_tk=abc_1; tracknick=tb88\n")
        self.assertTrue(self.acc.logged_in())
        self.assertEqual(self.acc.nick, "tb88")
        with self.assertRaises(ValueError):
            self.acc.use_cookie_text("cna=x; cookie2=y")

    def test_search_without_login_does_not_go_online(self):
        with mock.patch.object(xianyu, "account", self.acc), mock.patch.object(net, "post") as post:
            with self.assertRaises(xianyu.NeedLogin) as cm:
                xianyu.Xianyu().search(Query("CD"))
        post.assert_not_called()
        self.assertEqual(cm.exception.kind, "login")

    def test_token_refresh_then_success(self):
        """第一次没有令牌，接口回 TOKEN_EMPTY 并下发 _m_h5_tk；用新令牌重签一次就成功了。"""
        self.login()
        calls = []

        def fake_post(url, params=None, data=None, cookies=None, **kw):
            calls.append((params, data))
            if len(calls) == 1:
                cookies.set_cookie(xianyu.make_cookie("_m_h5_tk", "fresh_1790000000000"))
                return resp({"ret": ["FAIL_SYS_TOKEN_EMPTY::令牌为空"], "data": {}})
            return resp(SEARCH_OK)

        with mock.patch.object(xianyu, "account", self.acc), mock.patch.object(net, "post", fake_post):
            items = xianyu.Xianyu().search(Query("GYBE"))
        self.assertEqual(len(items), 2)
        (p1, d1), (p2, d2) = calls
        body = d2["data"]
        self.assertEqual(json.loads(body)["keyword"], "GYBE")
        self.assertEqual(json.loads(body)["sortField"], "create")
        want = hashlib.md5(f"fresh&{p2['t']}&{xianyu.APP_KEY}&{body}".encode()).hexdigest()
        self.assertEqual(p2["sign"], want)
        self.assertEqual(p1["sign"], hashlib.md5(f"&{p1['t']}&{xianyu.APP_KEY}&{body}".encode()).hexdigest())
        # 新令牌存下来了，重启以后不用再要一次
        again = xianyu.Account()
        again.attach(self.store)
        self.assertEqual(again.token(again.jar), "fresh")

    def test_errors(self):
        self.login()
        cases = [
            ({"ret": ["RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!"],
              "data": {"url": "https://passport.goofish.com/mini_login.htm?appEntrance=baxia"}}, xianyu.NeedLogin),
            ({"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]}, xianyu.NeedLogin),
            ({"ret": ["RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!"],
              "data": {"url": "https://h5api.m.goofish.com/_____tmd_____/punish?x5secdata=1"}}, net.Blocked),
            ({"ret": ["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]}, net.FetchError),
        ]
        for reply, err in cases:
            with self.subTest(reply["ret"][0]), mock.patch.object(net, "post", return_value=resp(reply)):
                with self.assertRaises(err) as cm:
                    self.acc.call(xianyu.SEARCH_API, {})
                if err is net.FetchError:
                    self.assertNotIsInstance(cm.exception, (xianyu.NeedLogin, net.Blocked))


VIEW = ('<html><script>window.viewData = {"loginFormData": {"appName": "xianyu", "fromSite": "77", '
        '"_csrf_token": "csrf1", "bizParams": "", "umidToken": "u1"}};</script></html>')


class QrLoginTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.acc = xianyu.Account()
        self.acc.attach(self.store)
        self.gets, self.posts = [], []
        self.statuses = []

    def tearDown(self):
        self.store.db.close()

    def fake_get(self, url, params=None, cookies=None, **kw):
        self.gets.append(url)
        if "mini_login" in url:
            cookies.set_cookie(xianyu.make_cookie("cookie2", "anon"))
            return resp(VIEW, url)
        return resp({"content": {"data": {"t": 1790000000123, "ck": "ck1",
                                          "codeContent": "https://passport.goofish.com/qrcodeCheck.htm?lgToken=x"},
                                 "success": True}}, url)

    def fake_post(self, url, data=None, cookies=None, **kw):
        self.posts.append(data)
        st = self.statuses.pop(0)
        if st == "CONFIRMED":
            cookies.set_cookie(xianyu.make_cookie("unb", "2200"))
            cookies.set_cookie(xianyu.make_cookie("tracknick", "tb88"))
        extra = {"iframeRedirect": True, "iframeRedirectUrl": "https://passport.goofish.com/iv"} if st == "VERIFY" else {}
        return resp({"content": {"data": {"qrCodeStatus": "CONFIRMED" if st == "VERIFY" else st, **extra}}}, url)

    def run_poll(self, *statuses):
        self.statuses = list(statuses)
        out = []
        with mock.patch.object(net, "get", self.fake_get), mock.patch.object(net, "post", self.fake_post):
            code = self.acc.qr_start()
            for _ in statuses:
                out.append(self.acc.qr_poll()["status"])
        return code, out

    def test_scan_and_confirm(self):
        code, out = self.run_poll("NEW", "SCANED", "CONFIRMED")
        self.assertIn("qrcodeCheck", code)
        self.assertEqual(out, ["new", "scanned", "done"])
        self.assertTrue(self.acc.logged_in())
        self.assertEqual(self.acc.nick, "tb88")
        # 生成二维码时带上了登录页的表单（空值也带着），轮询时再加上 t 和 ck
        gen = urllib.parse.parse_qs(urllib.parse.urlsplit(self.gets[1]).query, keep_blank_values=True)
        self.assertEqual(gen["_csrf_token"], ["csrf1"])
        self.assertEqual(gen["bizParams"], [""])
        self.assertEqual(gen["umidTag"], ["SERVER"])
        self.assertEqual((self.posts[0]["t"], self.posts[0]["ck"]), (1790000000123, "ck1"))
        self.assertEqual(self.acc.qr_poll()["status"], "none")   # 登录完就结束了

    def test_expired_and_verify(self):
        _, out = self.run_poll("EXPIRED")
        self.assertEqual(out, ["expired"])
        _, out = self.run_poll("VERIFY")
        self.assertEqual(out, ["verify"])
        self.assertFalse(self.acc.logged_in())

    def test_stale_poll_does_not_restore_a_canceled_login(self):
        def reload_account():
            self.acc.attach(self.store)

        for action in (self.acc.qr_cancel, self.acc.logout, reload_account):
            with self.subTest(action=action.__name__):
                self.acc.logout()

                def delayed_post(*args, **kwargs):
                    reply = self.fake_post(*args, **kwargs)
                    action()   # 请求在途时，用户取消或退出登录
                    return reply

                self.statuses = ["CONFIRMED"]
                with mock.patch.object(net, "get", self.fake_get), \
                        mock.patch.object(net, "post", delayed_post):
                    self.acc.qr_start()
                    self.assertEqual(self.acc.qr_poll()["status"], "none")
                self.assertFalse(self.acc.logged_in())
                again = xianyu.Account()
                again.attach(self.store)
                self.assertFalse(again.logged_in(), "迟到的扫码结果不能重新存入登录信息")

    def test_stale_poll_keeps_the_new_qr_session(self):
        def delayed_post(*args, **kwargs):
            reply = self.fake_post(*args, **kwargs)
            self.acc.qr_start()   # 另一个页面已经重新生成了二维码
            return reply

        self.statuses = ["CONFIRMED"]
        with mock.patch.object(net, "get", self.fake_get), mock.patch.object(net, "post", delayed_post):
            self.acc.qr_start()
            old = self.acc._qr
            self.assertEqual(self.acc.qr_poll()["status"], "none")
        self.assertIsNot(self.acc._qr, old)
        self.assertTrue(self.acc.state()["scanning"])
        self.assertFalse(self.acc.logged_in())

    def test_cookie_login_replaces_a_pending_qr_login(self):
        def delayed_post(*args, **kwargs):
            reply = self.fake_post(*args, **kwargs)
            self.acc.use_cookie_text("unb=3300; tracknick=imported")
            return reply

        self.statuses = ["CONFIRMED"]
        with mock.patch.object(net, "get", self.fake_get), mock.patch.object(net, "post", delayed_post):
            self.acc.qr_start()
            self.assertEqual(self.acc.qr_poll()["status"], "none")
        self.assertEqual(self.acc.nick, "imported")
        self.assertFalse(self.acc.state()["scanning"])
        again = xianyu.Account()
        again.attach(self.store)
        self.assertEqual(again.nick, "imported")

    def test_login_page_changed(self):
        with mock.patch.object(net, "get", return_value=resp("<html>改版了</html>")):
            with self.assertRaises(net.FetchError):
                self.acc.qr_start()


if __name__ == "__main__":
    unittest.main()
