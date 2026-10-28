"""新增渠道的请求格式、业务失败和长消息回归测试；不向真实账号发消息。"""

import json
import re
import unittest
import urllib.parse
from unittest import mock

from goodsmare import net, notify
from goodsmare.notify import Ctx, Hit
from goodsmare.sources import Item
from tests.test_notify import Capture, hits


NEW_CHANNELS = {
    "wxpusher": {"token": "SPT_test"},
    "xtuis": {"token": "test"},
    "iyuu": {"token": "IYUUtest"},
    "pushme": {"push_key": "test"},
    "discord": {"webhook": "https://discord.com/api/webhooks/1/test"},
    "pushover": {"token": "test", "user_key": "user"},
}


def response(body, status=200):
    raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    return net.Response(status, "https://example.test", {}, raw)


class ExtraNotifyTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Ctx(rate=0.05, page_url="http://pc:8787/p/AbCdEfGhIjKlMnOp")

    def test_every_channel_is_registered_and_checks_required_fields(self):
        self.assertEqual(set(notify.CHANNEL_TYPES), set(notify.SENDERS))
        for kind, fields in NEW_CHANNELS.items():
            with self.subTest(kind=kind), mock.patch.object(net, "post") as post:
                notify.validate_channel({"type": kind, **fields})
                with self.assertRaises(ValueError):
                    notify.send({"type": kind}, hits(), self.ctx)
                post.assert_not_called()

    def test_wxpusher_spt_html_and_buy_links(self):
        cap = Capture({"code": 1000, "data": [{"code": 1000, "status": "发送成功"}]})
        with mock.patch.object(net, "post", cap):
            notify.send({"type": "wxpusher", "token": "SPT_test"}, hits(2), Ctx(rate=0.05, buy="doorzo"))
        url, kw = cap.calls[0]
        body = kw["json_body"]
        self.assertEqual(url, "https://wxpusher.zjiecode.com/api/send/message/simple-push")
        self.assertEqual((body["spt"], body["contentType"]), ("SPT_test", 2))
        self.assertNotIn("appToken", body)
        self.assertIn("https://img/0.jpg", body["content"])
        self.assertIn('href="https://www.doorzo.cn/mall/surugaya/detail/0"', body["content"])
        self.assertEqual(body["url"], "https://www.doorzo.cn/mall/surugaya/detail/0")

    def test_wxpusher_multiple_spt_and_application_targets(self):
        cases = [
            ({"token": "SPT_one, SPT_two, SPT_one"}, {"sptList": ["SPT_one", "SPT_two"]}),
            ({"token": "AT_test", "uids": "UID_one，UID_two UID_one", "topic_ids": "12,34"},
             {"appToken": "AT_test", "uids": ["UID_one", "UID_two"], "topicIds": [12, 34]}),
            ({"token": "AT_test", "topic_ids": [12]}, {"appToken": "AT_test", "topicIds": [12]}),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                cap = Capture({"code": 1000})
                with mock.patch.object(net, "post", cap):
                    notify.send({"type": "wxpusher", **fields}, hits(), self.ctx)
                for key, value in expected.items():
                    self.assertEqual(cap.calls[0][1]["json_body"][key], value)
                expected_route = "/simple-push" if "sptList" in expected else "/message"
                self.assertTrue(cap.calls[0][0].endswith(expected_route))

    def test_wxpusher_large_photo_native_copy_and_doorzo_buttons(self):
        item = Item(source="mercari", id="m123", title="黑胶 & CD", price=3000,
                    url='https://jp.mercari.com/item/m123?from="push"&a=1',
                    image="https://static.mercdn.net/thumb/item/webp/m123_1.jpg?17", extra="包邮")
        cap = Capture({"code": 1000})
        with mock.patch.object(net, "post", cap):
            notify.send({"type": "wxpusher", "token": "SPT_test"},
                        [Hit(item, "w1", "黑胶")], Ctx(rate=0.05, buy="doorzo"))
        body = cap.calls[0][1]["json_body"]
        content = body["content"]
        self.assertIn('src="https://static.mercdn.net/item/detail/orig/photos/m123_1.jpg?17"', content)
        self.assertIn('width="320"', content)
        self.assertIn('<copy ', content)
        self.assertIn('data-clipboard-text="https://jp.mercari.com/item/m123?from=&quot;push&quot;&amp;a=1"', content)
        self.assertIn("复制原链接", content)
        self.assertIn("去任意门", content)
        self.assertIn("原商品页", content)
        self.assertIn("≈150元", content)
        self.assertNotIn("onclick=", content)
        self.assertNotIn("<script", content)
        self.assertEqual(body["url"], "https://www.doorzo.cn/mall/mercari/detail/m123")

    def test_wxpusher_doorzo_notification_link_wins_over_a_batch_page(self):
        ctx = Ctx(rate=0.05, buy="doorzo", page_url=self.ctx.page_url)
        cap = Capture({"code": 1000})
        with mock.patch.object(net, "post", cap):
            notify.send({"type": "wxpusher", "token": "SPT_test"}, hits(2), ctx)
        body = cap.calls[0][1]["json_body"]
        self.assertEqual(body["url"], "https://www.doorzo.cn/mall/surugaya/detail/0")
        self.assertIn(self.ctx.page_url, body["content"])

    def test_wxpusher_unsupported_source_keeps_original_and_copy_without_fake_image(self):
        item = Item(source="xianyu", id="870001", title="黑胶", price=380,
                    url="https://www.goofish.com/item?id=870001")
        cap = Capture({"code": 1000})
        with mock.patch.object(net, "post", cap):
            notify.send({"type": "wxpusher", "token": "SPT_test"},
                        [Hit(item, "w1", "黑胶")], Ctx(rate=0.05, buy="doorzo"))
        content = cap.calls[0][1]["json_body"]["content"]
        self.assertIn("复制原链接", content)
        self.assertIn('data-clipboard-text="https://www.goofish.com/item?id=870001"', content)
        self.assertIn("380元", content)
        self.assertNotIn("去任意门", content)
        self.assertNotIn("<img", content)
        self.assertEqual(cap.calls[0][1]["json_body"]["url"], item.url)

    def test_wxpusher_rejects_invalid_targets_before_sending(self):
        cases = [
            {"token": "bad"}, {"token": "SPT_"}, {"token": "AT_test"},
            {"token": "AT_test", "topic_ids": "text"}, {"token": "AT_test", "topic_ids": "0"},
            {"token": "AT_test", "topic_ids": "1,2,3,4,5,6"},
            {"token": ",".join(f"SPT_{i}" for i in range(11))},
        ]
        for fields in cases:
            with self.subTest(fields=fields), mock.patch.object(net, "post") as post:
                with self.assertRaises(ValueError):
                    notify.send({"type": "wxpusher", **fields}, hits(), self.ctx)
                post.assert_not_called()

    def test_wxpusher_detects_rejected_recipients(self):
        cap = Capture({"code": 1000, "data": [{"code": 1001, "status": "UID 不存在"}]})
        with mock.patch.object(net, "post", cap), self.assertRaisesRegex(RuntimeError, "UID 不存在"):
            notify.send({"type": "wxpusher", "token": "SPT_test"}, hits(), self.ctx)

    def test_wxpusher_splits_long_html_without_resetting_item_links(self):
        many = hits(220)
        for h in many:
            h.item.title = "中文商品名称" * 10
        cap = Capture({"code": 1000})
        with mock.patch.object(net, "post", cap):
            notify.send({"type": "wxpusher", "token": "SPT_test"}, many, self.ctx)
        self.assertGreater(len(cap.calls), 1)
        indices = []
        for _, kw in cap.calls:
            content = kw["json_body"]["content"]
            self.assertLessEqual(len(content), 40000)
            self.assertLessEqual(len(content.encode()), 65535)
            indices.extend(int(n) for n in re.findall(re.escape(self.ctx.page_url) + r'#i(\d+)"', content))
        self.assertEqual(list(dict.fromkeys(indices)), list(range(1, 221)))
        self.assertEqual(sum(c[1]["json_body"]["content"].count("<copy ") for c in cap.calls), 220)

    def test_xtuis_and_iyuu_send_markdown_as_form_data(self):
        cases = [
            ("xtuis", "test/key", "https://wx.xtuis.cn/test%2Fkey.send", {"msg_id": "wx_test"}),
            ("iyuu", "IYUUtest", "https://iyuu.cn/IYUUtest.send", {"errcode": 0}),
        ]
        for kind, token, url, reply in cases:
            with self.subTest(kind=kind):
                cap = Capture(reply)
                with mock.patch.object(net, "post", cap):
                    notify.send({"type": kind, "token": token}, hits(2), self.ctx)
                self.assertEqual(len(cap.calls), 1)
                self.assertEqual(cap.calls[0][0], url)
                data = cap.calls[0][1]["data"]
                self.assertIn("上新 2 件", data["text"])
                self.assertIn(self.ctx.page_url + "#i2", data["desp"])
                self.assertNotIn("json_body", cap.calls[0][1])

    def test_pushme_uses_html_and_accepts_only_success(self):
        with mock.patch.object(net, "post", return_value=response(" success\n")) as post:
            notify.send({"type": "pushme", "push_key": "key", "server": "https://push.example.test/"},
                        hits(2), self.ctx)
        self.assertEqual(post.call_args.args[0], "https://push.example.test")
        data = post.call_args.kwargs["data"]
        self.assertEqual((data["push_key"], data["type"]), ("key", "html"))
        self.assertIn(self.ctx.page_url + "#i2", data["content"])
        with mock.patch.object(net, "post", return_value=response("invalid push_key")), \
                self.assertRaisesRegex(RuntimeError, "invalid push_key"):
            notify.send({"type": "pushme", "push_key": "bad"}, hits(), self.ctx)
        with mock.patch.object(net, "post") as post, self.assertRaises(ValueError):
            notify.send({"type": "pushme", "push_key": "key", "server": "file:///tmp"}, hits(), self.ctx)
        post.assert_not_called()

    def test_new_json_channels_report_business_errors_and_invalid_json(self):
        cases = [
            ("wxpusher", {"code": 1001, "msg": "bad token"}),
            ("xtuis", {"code": 429, "msg": "limit", "msg_id": "not_a_success"}),
            ("iyuu", {"errcode": 400, "errmsg": "bad token"}),
            ("pushover", {"status": 0, "errors": ["invalid user"]}),
        ]
        for kind, reply in cases:
            for raw in (reply, "<html>login</html>", []):
                with self.subTest(kind=kind, reply=raw), \
                        mock.patch.object(net, "post", return_value=response(raw)), self.assertRaises(RuntimeError):
                    notify.send({"type": kind, **NEW_CHANNELS[kind]}, hits(), self.ctx)

    def test_discord_batches_cards_and_preserves_thread_and_page_offsets(self):
        cap = Capture({"id": "123"})
        channel = {"type": "discord", "webhook": "https://discord.com/api/webhooks/1/test?thread_id=42&wait=false"}
        with mock.patch.object(net, "post", cap):
            notify.send(channel, hits(23), self.ctx)
        self.assertEqual([len(c[1]["json_body"]["embeds"]) for c in cap.calls], [10, 10, 3])
        urls = []
        for url, kw in cap.calls:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertEqual(query, {"thread_id": ["42"], "wait": ["true"]})
            body = kw["json_body"]
            self.assertEqual(body["allowed_mentions"], {"parse": []})
            self.assertIn(self.ctx.page_url, body["content"])
            urls.extend(e["url"] for e in body["embeds"])
        self.assertEqual(urls, [self.ctx.page_url + f"#i{i}" for i in range(1, 24)])
        self.assertEqual(cap.calls[0][1]["json_body"]["embeds"][0]["thumbnail"]["url"], "https://img/0.jpg")
        self.assertEqual(cap.calls[0][1]["json_body"]["embeds"][0]["author"]["url"],
                         "https://www.suruga-ya.jp/product/detail/0")

    def test_discord_long_emoji_content_stays_under_limits(self):
        many = hits(10)
        for h in many:
            h.item.title = h.item.extra = h.watch_name = "😀" * 1000 + "@everyone"
        cap = Capture({"id": "123"})
        with mock.patch.object(net, "post", cap):
            notify.send({"type": "discord", **NEW_CHANNELS["discord"]}, many, self.ctx)
        body = cap.calls[0][1]["json_body"]
        self.assertLessEqual(len(body["content"].encode("utf-16-le")) // 2, 2000)
        text = "".join(e["title"] + e["description"] + e["footer"]["text"] for e in body["embeds"])
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 6000)
        self.assertTrue(all(len(e["title"].encode("utf-16-le")) // 2 <= 256 for e in body["embeds"]))

    def test_discord_honors_short_rate_limits_but_does_not_wait_forever(self):
        channel = {"type": "discord", **NEW_CHANNELS["discord"]}
        with mock.patch.object(net, "post", side_effect=[response({"retry_after": 0.2}, 429),
                                                         response({"id": "123"})]) as post, \
                mock.patch.object(notify.time, "sleep") as sleep:
            notify.send(channel, hits(), self.ctx)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(0.25)
        with mock.patch.object(net, "post", return_value=response({"retry_after": 3600}, 429)) as post, \
                mock.patch.object(notify.time, "sleep") as sleep, self.assertRaises(net.FetchError):
            notify.send(channel, hits(), self.ctx)
        post.assert_called_once()
        sleep.assert_not_called()

    def test_pushover_splits_at_1024_and_preserves_every_item(self):
        cap = Capture({"status": 1})
        channel = {"type": "pushover", **NEW_CHANNELS["pushover"], "device": "phone", "sound": "cashregister"}
        with mock.patch.object(net, "post", cap):
            notify.send(channel, hits(30), self.ctx)
        self.assertGreater(len(cap.calls), 1)
        indices = []
        for url, kw in cap.calls:
            data = kw["data"]
            self.assertEqual(url, "https://api.pushover.net/1/messages.json")
            self.assertEqual((data["device"], data["sound"]), ("phone", "cashregister"))
            self.assertLessEqual(len(data["message"]), 1024)
            self.assertLessEqual(len(data["title"]), 250)
            indices.extend(int(i) for i in re.findall(re.escape(self.ctx.page_url) + r'#i(\d+)', data["message"]))
        self.assertEqual(indices, list(range(1, 31)))

    def test_all_new_channels_display_cny_without_converting_it_again(self):
        hit = Hit(Item(source="xianyu", id="1", title="黑胶", price=380,
                       url="https://www.goofish.com/item?id=1"), "w1", "黑胶")
        replies = {"wxpusher": {"code": 1000}, "xtuis": {"code": 200}, "iyuu": {"errcode": 0},
                   "pushme": "success", "discord": {"id": "123"}, "pushover": {"status": 1}}
        for kind, fields in NEW_CHANNELS.items():
            with self.subTest(kind=kind), mock.patch.object(net, "post", return_value=response(replies[kind])) as post:
                notify.send({"type": kind, **fields}, [hit], self.ctx)
                sent = json.dumps(post.call_args.kwargs, ensure_ascii=False)
                self.assertIn("380元", sent)
                self.assertNotIn("¥380", sent)

    def test_a_failed_new_channel_does_not_stop_other_channels(self):
        def post(url, **kw):
            if "iyuu.cn" in url:
                return response({"errcode": 400, "errmsg": "rejected"})
            return response({"code": 1000})

        with mock.patch.object(net, "post", post):
            failures = notify.send_all([{"type": "iyuu", **NEW_CHANNELS["iyuu"]},
                                        {"type": "wxpusher", **NEW_CHANNELS["wxpusher"]}], hits(), self.ctx)
        self.assertEqual(len(failures), 1)
        self.assertIn("rejected", failures[0][1])


if __name__ == "__main__":
    unittest.main()
