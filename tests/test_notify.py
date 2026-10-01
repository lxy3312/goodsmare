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
        calls = self.send({"type": "wecom", "webhook": "https://qyapi.weixin.qq.com/x", "rich": False}, n=10)
        self.assertEqual([len(c[1]["json_body"]["news"]["articles"]) for c in calls], [8, 2])
        art = calls[0][1]["json_body"]["news"]["articles"][0]
        self.assertEqual(art["picurl"], "https://img/0.jpg")
        self.assertEqual(art["url"], "https://www.suruga-ya.jp/product/detail/0")

    def test_wecom_shows_price_and_big_first_image(self):
        calls = self.send({"type": "wecom", "webhook": "https://qyapi.weixin.qq.com/x", "rich": False}, n=2)
        arts = calls[0][1]["json_body"]["news"]["articles"]
        # 多篇图文只显示标题，价格得写进标题
        self.assertEqual(arts[0]["title"], "【骏河屋】¥1,000 缶バッジ 0")
        self.assertIn("≈50元", arts[0]["description"])
        self.assertIn("中古", arts[0]["description"])

    def test_wecom_one_message_with_everything(self):
        many = hits(2)
        many[0].item.title = "【美品】*G-SHOCK* [限定] #1"
        cap = Capture()
        with mock.patch.object(net, "post", cap):
            notify.send({"type": "wecom", "webhook": "https://qyapi.weixin.qq.com/x"}, many, CTX)
        self.assertEqual([c[1]["json_body"]["msgtype"] for c in cap.calls], ["markdown_v2"], "一次推送就一条")
        md = cap.calls[0][1]["json_body"]["markdown_v2"]["content"]
        self.assertTrue(md.startswith("### 五条悟吧唧 上新 2 件"))
        self.assertIn("**【骏河屋】【美品】＊G-SHOCK＊ ［限定］ ＃1**", md, "标题里的 markdown 符号换成全角")
        self.assertIn("![](https://img/0.jpg)", md)
        self.assertIn("**¥1,000（≈50元）** · 中古", md)
        self.assertIn("[https://www.suruga-ya.jp/product/detail/0](https://www.suruga-ya.jp/product/detail/0)", md,
                      "原链接原样显示：点得开，长按也能复制")
        self.assertNotIn("关注：", md, "只有一个关注时不用每件都写")
        # 关掉以后改回一件一张的图文卡片
        calls = self.send({"type": "wecom", "webhook": "https://qyapi.weixin.qq.com/x", "rich": False})
        self.assertEqual([c[1]["json_body"]["msgtype"] for c in calls], ["news"])

    def test_wecom_falls_back_to_cards_when_markdown_v2_is_unknown(self):
        def reply(url):
            return {"errcode": 0}
        cap = Capture(reply)

        def post(url, **kw):
            cap(url, **kw)
            body = {"errcode": 40008, "errmsg": "invalid message type"} if kw["json_body"]["msgtype"] == "markdown_v2" \
                else {"errcode": 0}
            return net.Response(200, url, {"Content-Type": "application/json"}, json.dumps(body).encode())
        with mock.patch.object(net, "post", post):
            notify.send({"type": "wecom", "webhook": "https://qyapi.weixin.qq.com/x"}, hits(2), CTX)
        self.assertEqual([c[1]["json_body"]["msgtype"] for c in cap.calls], ["markdown_v2", "news"])

    def test_wecom_markdown_is_split_under_the_limit(self):
        many = hits(40)
        mds = notify.wecom_markdown(many, Ctx(rate=0.05, page_url="http://pc:8787/p/AbCdEfGhIjKlMnOp"))
        self.assertGreater(len(mds), 1)
        self.assertTrue(all(len(m.encode("utf-8")) <= 4096 for m in mds))
        self.assertIn("(http://pc:8787/p/AbCdEfGhIjKlMnOp)", mds[0])
        joined = "\n".join(mds)
        for h in many:
            self.assertIn(f"[{h.item.url}]", joined)

    def test_page_links(self):
        ctx = Ctx(rate=0.05, web_url="http://pc:8787", page_url="http://pc:8787/p/AbCdEfGhIjKlMnOp")
        def sent(ch, n=1):
            cap = Capture({"code": 200, "errcode": 0, "ok": True})
            with mock.patch.object(net, "post", cap):
                notify.send(ch, hits(n), ctx)
            return cap.calls
        wecom, = sent({"type": "wecom", "webhook": "https://qyapi.weixin.qq.com/x", "rich": False}, 2)
        md, = sent({"type": "wecom", "webhook": "https://qyapi.weixin.qq.com/x"}, 2)
        bark = sent({"type": "bark", "key": "K"}, notify.PER_ITEM_LIMIT + 2)
        ntfy, = sent({"type": "ntfy", "topic": "t"})
        sc, = sent({"type": "serverchan", "sendkey": "SCT1"})
        hook, = sent({"type": "webhook", "url": "http://x"})
        self.assertIn(f"]({ctx.page_url})", md[1]["json_body"]["markdown_v2"]["content"])
        arts = wecom[1]["json_body"]["news"]["articles"]
        self.assertEqual([a["url"] for a in arts], [ctx.page_url + "#i1", ctx.page_url + "#i2"])
        self.assertEqual(bark[0][1]["json_body"]["url"], ctx.page_url + "#i1")
        self.assertEqual(bark[-1][1]["json_body"]["url"], ctx.page_url + f"#i{notify.PER_ITEM_LIMIT + 1}")
        body = ntfy[1]["json_body"]
        self.assertEqual(body["click"], ctx.page_url + "#i1")
        self.assertEqual(body["actions"][0]["url"], "https://www.suruga-ya.jp/product/detail/0", "还能直达原商品页")
        self.assertTrue(sc[1]["json_body"]["desp"].startswith(f"[{notify.PAGE_LABEL}]({ctx.page_url})"))
        self.assertIn("打开商品页](https://www.suruga-ya.jp/product/detail/0)", sc[1]["json_body"]["desp"])
        self.assertEqual(hook[1]["json_body"]["page_url"], ctx.page_url)
        self.assertEqual(hook[1]["json_body"]["items"][0]["link"], ctx.page_url + "#i1")

    def test_no_page_means_direct_links(self):
        calls = self.send({"type": "ntfy", "topic": "t"})
        body = calls[0][1]["json_body"]
        self.assertEqual(body["click"], "https://www.suruga-ya.jp/product/detail/0")
        self.assertNotIn("actions", body)

    def test_photo_is_the_big_version(self):
        def p(url, source):
            return notify.photo(Item(source=source, id="1", title="t", price=1, url="u", image=url))
        # 2026 年 9 月逐个试过能打开、都是 JPG
        self.assertEqual(p("https://static.mercdn.net/thumb/item/webp/m9_1.jpg?17", "mercari"),
                         "https://static.mercdn.net/item/detail/orig/photos/m9_1.jpg?17")
        self.assertEqual(p("https://assets.mercari-shops-static.com/-/small/plain/2JX.jpg@webp", "mercari"),
                         "https://assets.mercari-shops-static.com/-/large/plain/2JX.jpg@jpg")
        self.assertIn("?pri=l&w=800&h=800&", p("https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/a/i-img.jpg"
                                                "?pri=s&w=298&h=298&ccw=298&cch=298&fill=1", "yahoo_flea"))
        self.assertEqual(p("https://img.fril.jp/img/85/m/29.jpg?1", "rakuma"), "https://img.fril.jp/img/85/l/29.jpg?1")
        self.assertEqual(p("https://img.mandarake.co.jp/webshopimg/01/00/389/0100684389/s_0100.jpg", "mandarake"),
                         "https://img.mandarake.co.jp/webshopimg/01/00/389/0100684389/0100.jpg")
        self.assertEqual(p("https://tc-animate.techorus-cdn.com/resize_image/resize_image.php?image=a.jpg&width=400&height=400&square=1",
                           "animate"),
                         "https://tc-animate.techorus-cdn.com/resize_image/resize_image.php?image=a.jpg&width=800&height=800&square=1")
        self.assertEqual(p("", "rakuma"), "")

    def test_bark_per_item_with_overflow_summary(self):
        calls = self.send({"type": "bark", "key": "K"}, n=notify.PER_ITEM_LIMIT + 3, reply={"code": 200})
        self.assertEqual(len(calls), notify.PER_ITEM_LIMIT + 1)
        self.assertEqual(calls[0][0], "https://api.day.app/push")
        self.assertEqual(calls[0][1]["json_body"]["url"], "https://www.suruga-ya.jp/product/detail/0")
        self.assertEqual(calls[0][1]["json_body"]["copy"], "https://www.suruga-ya.jp/product/detail/0")
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
