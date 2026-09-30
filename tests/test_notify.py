import base64
import hashlib
import hmac
import json
import unittest
import urllib.parse
from unittest import mock

from goodsmare import net, notify
from goodsmare.notify import Ctx, Hit
from goodsmare.sources import Item


def hits(n=1):
    return [Hit(Item(source="surugaya", id=str(i), title=f"缶バッジ {i}", price=1000 + i,
                     url=f"https://www.suruga-ya.jp/product/detail/{i}", image=f"https://img/{i}.jpg",
                     extra="中古"), "w1", "五条悟吧唧") for i in range(n)]


class Capture:
    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or {"code": 0, "errcode": 0, "ok": True}

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        reply = self.reply(url) if callable(self.reply) else self.reply
        return net.Response(200, url, {"Content-Type": "application/json"}, json.dumps(reply).encode())


CTX = Ctx(rate=0.05)


class NotifyTests(unittest.TestCase):
    def send(self, ch, n=1, reply=None):
        cap = Capture(reply)
        with mock.patch.object(net, "post", cap):
            notify.send(ch, hits(n), CTX)
        return cap.calls

    def test_serverchan_turbo_and_sc3(self):
        calls = self.send({"type": "serverchan", "sendkey": "SCT123abc"})
        self.assertEqual(calls[0][0], "https://sctapi.ftqq.com/SCT123abc.send")
        body = calls[0][1]["json_body"]
        self.assertIn("五条悟吧唧", body["title"])
        self.assertIn("≈50元", body["desp"])
        self.assertIn("![](https://img/0.jpg)", body["desp"])
        self.assertIn("[👉 打开商品页](https://www.suruga-ya.jp/product/detail/0)", body["desp"])
        self.assertIn("缶バッジ 0", body["short"])
        calls = self.send({"type": "serverchan", "sendkey": "sctp4567tABCDEF"})
        self.assertEqual(calls[0][0], "https://4567.push.ft07.com/send/sctp4567tABCDEF.send")

    def test_one_message_per_cycle_for_summary_channels(self):
        calls = self.send({"type": "pushplus", "token": "t"}, n=5, reply={"code": 200})
        self.assertEqual(len(calls), 1)
        body = calls[0][1]["json_body"]
        self.assertEqual(body["template"], "html")
        self.assertIn('<img src="https://img/4.jpg"', body["content"])
        self.assertIn('href="https://www.suruga-ya.jp/product/detail/4"', body["content"])
        self.assertIn("打开商品页", body["content"])

    def test_cover_turns_webp_into_jpg(self):
        def c(url, source="mercari", id="m1"):
            return notify.cover(Item(source=source, id=id, title="t", price=1, url="u", image=url))
        # 2026 年 9 月煤炉接口实际给的地址
        self.assertEqual(c("https://static.mercdn.net/thumb/item/webp/m71259340700_1.jpg?1784458467"),
                         "https://static.mercdn.net/thumb/item/jpeg/m71259340700_1.jpg?1784458467")
        self.assertEqual(c("https://static.mercdn.net/item/detail/webp/photos/m7_1.jpg?17"),
                         "https://static.mercdn.net/item/detail/orig/photos/m7_1.jpg?17")
        self.assertEqual(c("https://assets.mercari-shops-static.com/-/small/plain/2JXNZChmoeUyj9sWuoQZQ4.webp@webp"),
                         "https://assets.mercari-shops-static.com/-/small/plain/2JXNZChmoeUyj9sWuoQZQ4.webp@jpg")
        self.assertEqual(c("https://www.suruga-ya.jp/database/photo.php?shinaban=646176708&size=m", "surugaya", "646176708"),
                         "https://www.suruga-ya.jp/database/pics_light/game/646176708.jpg")
        self.assertEqual(c("https://img.mandarake.co.jp/shopimg/s_1.jpg", "mandarake", "1"),
                         "https://img.mandarake.co.jp/shopimg/s_1.jpg")
        self.assertEqual(c("https://static.mercdn.net/c!/w=240,f=webp/thumb/photos/m1_1.jpg?17"),
                         "https://static.mercdn.net/c!/w=240/thumb/photos/m1_1.jpg?17")
        self.assertEqual(c("https://static.mercdn.net/c!/f=webp/thumb/photos/m1_1.jpg"),
                         "https://static.mercdn.net/thumb/photos/m1_1.jpg")
        self.assertEqual(c("https://static.mercdn.net/c!/w=240/thumb/photos/m1_1.jpg"),
                         "https://static.mercdn.net/c!/w=240/thumb/photos/m1_1.jpg")
        self.assertEqual(c("https://auc-pctr.c.yimg.jp/i/a.jpg?w=300"), "https://auc-pctr.c.yimg.jp/i/a.jpg?w=300")
        self.assertEqual(c("data:image/svg+xml;utf8,<svg/>"), "")
        self.assertEqual(c(""), "")

    def test_markdown_keeps_link_out_of_reference_syntax(self):
        text = notify.markdown(hits(1), 0.05)
        self.assertTrue(text.startswith("**【骏河屋】缶バッジ 0**"))
        self.assertNotIn("] [", text)
        # Server酱只认空行换行：每一部分之间都是空行
        self.assertEqual(text.split("\n\n")[-1], "[👉 打开商品页](https://www.suruga-ya.jp/product/detail/0)")

    def test_wecom_chunks_articles_by_8(self):
        calls = self.send({"type": "wecom", "webhook": "https://qyapi.weixin.qq.com/x"}, n=10)
        self.assertEqual([len(c[1]["json_body"]["news"]["articles"]) for c in calls], [8, 2])
        art = calls[0][1]["json_body"]["news"]["articles"][0]
        self.assertEqual(art["picurl"], "https://img/0.jpg")
        self.assertEqual(art["url"], "https://www.suruga-ya.jp/product/detail/0")

    def test_bark_per_item_with_overflow_summary(self):
        calls = self.send({"type": "bark", "key": "K"}, n=notify.PER_ITEM_LIMIT + 3, reply={"code": 200})
        self.assertEqual(len(calls), notify.PER_ITEM_LIMIT + 1)
        self.assertEqual(calls[0][0], "https://api.day.app/push")
        self.assertEqual(calls[0][1]["json_body"]["url"], "https://www.suruga-ya.jp/product/detail/0")
        self.assertIn("还有 3 件", calls[-1][1]["json_body"]["title"])

    def test_dingtalk_sign(self):
        with mock.patch("time.time", return_value=1700000000.0):
            calls = self.send({"type": "dingtalk", "webhook": "https://oapi.dingtalk.com/robot/send?access_token=a",
                               "secret": "SECabc"})
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(calls[0][0]).query))
        expect = base64.b64encode(hmac.new(b"SECabc", b"1700000000000\nSECabc", hashlib.sha256).digest()).decode()
        self.assertEqual(q["timestamp"], "1700000000000")
        self.assertEqual(q["sign"], expect)
        self.assertEqual(calls[0][1]["json_body"]["msgtype"], "markdown")

    def test_feishu_sign(self):
        with mock.patch("time.time", return_value=1700000000.0):
            calls = self.send({"type": "feishu", "webhook": "https://open.feishu.cn/x", "secret": "s"},
                              reply={"code": 0})
        body = calls[0][1]["json_body"]
        expect = base64.b64encode(hmac.new(b"1700000000\ns", b"", hashlib.sha256).digest()).decode()
        self.assertEqual(body["sign"], expect)
        self.assertEqual(body["msg_type"], "post")

    def test_telegram_falls_back_to_text_when_photo_fails(self):
        def reply(url):
            return {"ok": False} if url.endswith("sendPhoto") else {"ok": True}

        cap = Capture(reply)

        def post(url, **kw):
            r = cap(url, **kw)
            if url.endswith("sendPhoto"):
                r.status = 400
            return r
        with mock.patch.object(net, "post", post):
            notify.send({"type": "telegram", "token": "T", "chat_id": "1"}, hits(1), CTX)
        self.assertEqual([c[0].rsplit("/", 1)[1] for c in cap.calls], ["sendPhoto", "sendMessage"])

    def test_api_error_is_raised(self):
        with self.assertRaises(RuntimeError):
            self.send({"type": "wecom", "webhook": "https://x"}, reply={"errcode": 93000, "errmsg": "invalid"})

    def test_missing_fields(self):
        with self.assertRaises(ValueError) as cm:
            notify.send({"type": "telegram", "token": "T"}, hits(1), CTX)
        self.assertIn("Chat ID", str(cm.exception))

    def test_send_all_isolates_failures(self):
        ok = Capture()
        with mock.patch.object(net, "post", ok):
            failures = notify.send_all([{"type": "bark", "name": "坏的"},
                                        {"type": "webhook", "url": "http://x"}], hits(2), CTX)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], "坏的")
        self.assertEqual(ok.calls[0][1]["json_body"]["items"][0]["price_cny"], 50.0)

    def test_email_builds_message(self):
        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, context=None, timeout=None):
                sent["host"] = (host, port)

            def login(self, u, p):
                sent["login"] = u

            def sendmail(self, frm, to, msg):
                sent["to"] = to
                sent["msg"] = msg

            def quit(self):
                pass
        with mock.patch("smtplib.SMTP_SSL", FakeSMTP):
            notify.send({"type": "email", "host": "smtp.qq.com", "user": "me@qq.com", "password": "p"},
                        hits(2), CTX)
        self.assertEqual(sent["host"], ("smtp.qq.com", 465))
        self.assertEqual(sent["to"], ["me@qq.com"])
        self.assertIn("Subject:", sent["msg"])


if __name__ == "__main__":
    unittest.main()
