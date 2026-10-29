"""消息积木：配置隔离、内容选择、渠道降级和拆分后链接；全部使用假网络。"""
import copy
import json
import unittest
from email import message_from_string
from unittest import mock

from goodsmare import message_layout as layout, net, notify
from goodsmare.config import normalize, normalize_channel
from goodsmare.notify import Ctx, Hit
from goodsmare.sources import Item, Query
from tests.test_notify import Capture, hits
from tests.test_notify_extra import NEW_CHANNELS, response


FIELDS = {
    "serverchan": {"sendkey":"SCT_test"}, "pushplus":{"token":"test"},
    "wecom":{"webhook":"https://example.test/wecom"}, "bark":{"key":"test"},
    "dingtalk":{"webhook":"https://example.test/dingtalk"}, "feishu":{"webhook":"https://example.test/feishu"},
    "telegram":{"token":"test","chat_id":"123"}, "ntfy":{"topic":"test"},
    "email":{"host":"example.test","user":"test@example.test","password":"test"},
    "webhook":{"url":"https://example.test/hook"}, **NEW_CHANNELS,
}


def settings(**values):
    return layout.normalize(values)


class LayoutTests(unittest.TestCase):
    def test_normalization_preserves_order_and_validates_values(self):
        self.assertIsNone(normalize({})["message"])
        actual = settings(blocks=["note", "title", "note", "bad", {}], title_length="999", max_items=-5,
                          image_size="giant", click_target="javascript:bad", enabled="off", note="x" * 700)
        self.assertEqual(actual["blocks"], ["note", "title"])
        self.assertEqual((actual["title_length"],actual["max_items"]), (500,0))
        self.assertEqual((actual["image_size"],actual["click_target"]), ("large","inherit"))
        self.assertFalse(actual["enabled"])
        self.assertEqual(len(actual["note"]),500)
        self.assertEqual(settings(blocks=[])["blocks"],[])
        self.assertNotIn("message",normalize_channel({"type":"wxpusher","message":None}))

    def test_schema_and_presets_are_independent(self):
        schema = layout.schema()
        self.assertEqual(len(schema["blocks"]),24)
        self.assertEqual(set(schema["capabilities"]),set(notify.CHANNEL_TYPES))
        for preset in schema["presets"].values():
            self.assertEqual(settings(**preset["settings"]),preset["settings"])
        schema["defaults"]["blocks"].clear()
        schema["presets"]["shopping"]["settings"]["blocks"].clear()
        self.assertTrue(layout.DEFAULT_LAYOUT["blocks"])
        self.assertTrue(layout.PRESETS["shopping"]["settings"]["blocks"])

    def test_global_inheritance_override_and_disable(self):
        global_layout = settings(blocks=["title"])
        self.assertEqual(layout.effective(global_layout,{}),global_layout)
        self.assertEqual(layout.effective(global_layout,{"message":settings(blocks=["price"])})["blocks"],["price"])
        self.assertIsNone(layout.effective(global_layout,{"message":{"enabled":False}}))
        inherited = layout.effective(global_layout,{})
        inherited["blocks"].clear()
        self.assertEqual(global_layout["blocks"],["title"])

    def test_conditional_blocks_and_escaped_templates(self):
        draft = settings(blocks=["note","title","cny","drop","published","buy_button","copy_original","divider"],
                         note="{标题} / {未知变量} / {__class__}")
        data = {"title":"<script>alert(1)</script>","original_url":'https://example.test/?a="&b=2'}
        html = layout.render(data,draft,"wxpusher")
        self.assertNotIn("<script",html)
        self.assertIn("&lt;script&gt;",html)
        self.assertIn("{未知变量}",html)
        self.assertIn("{__class__}",html)
        self.assertIn('data-clipboard-text="https://example.test/?a=&quot;&amp;b=2"',html)
        keys = [s["key"] for s in layout.sections(data,draft)]
        self.assertEqual(keys,["note","title","copy_original","divider"])
        self.assertNotIn("<copy",layout.render(data,draft,"email"))
        self.assertIn("长按复制",layout.render(data,draft,"email"))
        self.assertNotIn("javascript:",layout.render({**data,"original_url":"javascript:alert(1)"},draft,"wxpusher"))

    def test_custom_title_variables_and_real_currency(self):
        fish = Hit(Item(source="xianyu",id="123",title="周边",price=88,url="https://www.goofish.com/item?id=123"),"w1","闲鱼")
        ctx = Ctx(rate=.05,message=settings(blocks=["price","cny","buy_button","drop"],title_template="{关注} · {数量} · {价格}"))
        self.assertEqual(notify.summary_title([fish,fish],ctx),"闲鱼 · 2 · 88元")
        data = notify._message_data(fish,ctx)
        self.assertEqual((data["cny"],data["buy_url"],data["drop"]),("","",""))

    def test_small_thumbnail_keeps_original_image_and_search_filters(self):
        h = hits()[0]
        h.item.source = "mercari"
        h.item.image = "https://static.mercdn.net/thumb/item/webp/m123_1.jpg"
        query = Query("缶バッジ",price_min=1000,price_max=3000,exclude=["空箱"])
        ctx = Ctx(rate=.05,message=settings(image_size="small",blocks=["image","image_button","search_button"]),
                  keywords={"w1":query.keyword},queries={"w1":query})
        data = notify._message_data(h,ctx)
        self.assertIn("thumb",data["image"])
        self.assertIn("/detail/orig/",data["full_image"])
        self.assertIn("price_min=1000",data["search_url"])
        self.assertIn("price_max=3000",data["search_url"])
        html = layout.render(data,ctx.message,"wxpusher")
        self.assertIn('width="96"',html)
        self.assertIn('href="'+data["full_image"]+'"',html)

    def test_every_sender_uses_selected_content_and_no_unselected_metadata(self):
        for kind, fields in FIELDS.items():
            with self.subTest(kind=kind):
                calls = []
                def post(url, calls=calls, kind=kind, **kwargs):
                    calls.append(kwargs)
                    if kind == "pushme": return response("success")
                    return response({"code":1000 if kind == "wxpusher" else 200 if kind in ("pushplus","bark") else 0,
                                     "errcode":0,"ok":True,"status":1,"msg_id":"test"})
                mail = mock.MagicMock()
                ctx = Ctx(rate=.05,message=settings(blocks=["note"],note="只有这段自定义文字"))
                with mock.patch.object(net,"post",side_effect=post), mock.patch.object(notify.smtplib,"SMTP_SSL",return_value=mail):
                    notify.send({"type":kind,**fields},hits(),ctx)
                if kind == "email":
                    msg = message_from_string(mail.sendmail.call_args.args[2])
                    text = msg.get_payload(decode=True).decode()
                elif kind == "webhook":
                    payload = calls[0]["json_body"]
                    self.assertEqual(len(payload["items"]),1)  # 接口结构化商品信息完整保留
                    text = payload["text"]
                else:
                    text = json.dumps(calls,ensure_ascii=False)
                self.assertIn("只有这段自定义文字",text)
                for absent in ("缶バッジ","五条悟吧唧","¥1,000","中古","https://img/"):
                    self.assertNotIn(absent,text)

    def test_channel_overrides_do_not_mutate_shared_context(self):
        ctx = Ctx(rate=.05,message=settings(blocks=["title"]))
        original = copy.deepcopy(ctx.message)
        cap = Capture({"code":1000})
        with mock.patch.object(net,"post",cap):
            notify.send({"type":"wxpusher","token":"SPT_test","message":settings(blocks=["price"])},hits(),ctx)
            notify.send({"type":"wxpusher","token":"SPT_test"},hits(),ctx)
        self.assertNotIn("缶バッジ",cap.calls[0][1]["json_body"]["content"])
        self.assertIn("缶バッジ",cap.calls[1][1]["json_body"]["content"])
        self.assertEqual(ctx.message,original)

    def test_custom_maximum_splits_without_losing_page_offsets(self):
        ctx = Ctx(rate=.05,page_url="http://pc/p/abcd",message=settings(blocks=["title","page_button"],max_items=2,click_target="page"))
        cap = Capture({"code":1000})
        with mock.patch.object(net,"post",cap):
            notify.send({"type":"wxpusher","token":"SPT_test"},hits(5),ctx)
        self.assertEqual(len(cap.calls),3)
        contents = "".join(kw["json_body"]["content"] for _,kw in cap.calls)
        for i in range(1,6): self.assertIn(f"#i{i}",contents)
        self.assertNotIn("#i6",contents)
        self.assertEqual(cap.calls[-1][1]["json_body"]["url"],ctx.page_url+"#i5")

    def test_explicit_click_target_wins_over_default_for_batch(self):
        for target in ("original","doorzo","page"):
            with self.subTest(target=target):
                ctx = Ctx(rate=.05,buy="",page_url="http://pc/p/abcd",message=settings(click_target=target))
                cap = Capture({"code":1000})
                with mock.patch.object(net,"post",cap):
                    notify.send({"type":"wxpusher","token":"SPT_test"},hits(2),ctx)
                expected = {"original":hits()[0].item.url,"doorzo":notify.buy_url(hits()[0].item,"doorzo"),"page":ctx.page_url}[target]
                self.assertEqual(cap.calls[0][1]["json_body"]["url"],expected)

    def test_system_notification_overflow_keeps_selected_information(self):
        for kind in ("bark","ntfy"):
            with self.subTest(kind=kind):
                cap = Capture({"code":200})
                ctx = Ctx(rate=.05,message=settings(blocks=["title","original_button"]))
                with mock.patch.object(net,"post",cap):
                    notify.send({"type":kind,**FIELDS[kind]},hits(9),ctx)
                all_content = json.dumps(cap.calls,ensure_ascii=False)
                for i in range(9): self.assertIn(f"缶バッジ {i}",all_content)
                self.assertNotIn('"image"',all_content)
                self.assertNotIn('"attach"',all_content)
                self.assertNotIn('"copy"',all_content)

    def test_telegram_long_modules_split_with_balanced_html(self):
        cap = Capture({"ok":True})
        ctx = Ctx(rate=.05,message=settings(blocks=["note","note2"],note="<"*500,note2="&"*500))
        with mock.patch.object(net,"post",cap):
            notify.send({"type":"telegram",**FIELDS["telegram"]},hits(),ctx)
        self.assertEqual(len(cap.calls),2)
        for url, kw in cap.calls:
            body = kw["json_body"]
            self.assertTrue(url.endswith("sendMessage"))
            self.assertLessEqual(len(body["text"]),3800)
            self.assertNotIn("<script",body["text"])

    def test_pushover_single_long_item_is_split_without_losing_text(self):
        cap = Capture({"status":1})
        ctx = Ctx(rate=.05,message=settings(blocks=["note","note2","title","watch","extra","price"],note="甲"*500,note2="乙"*500))
        with mock.patch.object(net,"post",cap):
            notify.send({"type":"pushover",**FIELDS["pushover"]},hits(),ctx)
        self.assertGreater(len(cap.calls),1)
        texts = [kw["data"]["message"] for _,kw in cap.calls]
        self.assertTrue(all(len(t)<=1024 for t in texts))
        self.assertEqual("".join(texts).count("甲"),500)
        self.assertEqual("".join(texts).count("乙"),500)

    def test_discord_splits_by_total_embed_limit(self):
        cap = Capture({"id":"1"})
        ctx = Ctx(rate=.05,message=settings(blocks=["note","note2","title"],note="甲"*500,note2="乙"*500))
        with mock.patch.object(net,"post",cap):
            notify.send({"type":"discord",**FIELDS["discord"]},hits(12),ctx)
        self.assertGreater(len(cap.calls),2)
        self.assertEqual(sum(len(kw["json_body"]["embeds"]) for _,kw in cap.calls),12)
        for _,kw in cap.calls:
            body = kw["json_body"]
            self.assertLessEqual(sum(len((e["title"]+e["description"]).encode("utf-16-le"))//2 for e in body["embeds"]),6000)
            self.assertEqual(body["allowed_mentions"],{"parse":[]})

    def test_capability_warnings_explain_unavailable_controls(self):
        draft = settings(blocks=["image","copy_original","manage_button"],click_target="page")
        text = " ".join(layout.warnings(draft,"feishu"))
        for word in ("图片链接","长按复制","手机访问"): self.assertIn(word,text)
        image_only = layout.render({"image":"https://example.test/1.jpg"},settings(blocks=["image"]),"bark","text")
        self.assertIn("消息附件",image_only)
        self.assertNotIn("没有",image_only)

    def test_long_wecom_content_splits_at_modules_and_keeps_next_item_index(self):
        ctx = Ctx(rate=.05,page_url="http://pc/p/abcd",message=settings(
            blocks=["note","note2","title","page_button"],note="甲"*500,note2="乙"*500,click_target="page"))
        hs = hits(2)
        hs[0].item.title = "很长的商品标题" * 70
        ctx.message["title_length"] = 500
        cap = Capture({"errcode":0})
        with mock.patch.object(net,"post",cap):
            notify.send({"type":"wecom",**FIELDS["wecom"]},hs,ctx)
        contents = [kw["json_body"]["markdown_v2"]["content"] for _,kw in cap.calls]
        self.assertGreater(len(contents),1)
        self.assertTrue(all(len(s.encode("utf-8"))<=4096 for s in contents))
        self.assertEqual("".join(contents).count("甲"),1000)
        batches = list(notify._message_batches(hs,ctx,"markdown",limit=1800,max_bytes=3500))
        self.assertEqual(batches[-1][0],1)
        self.assertIn("#i2",batches[-1][2])


    def test_price_says_drop_when_drop_blocks_are_not_chosen(self):
        """选了价格、没选「上新 / 降价」和「降价幅度」：降价的商品在价格里标出来，不然和上新看不出区别。"""
        h = Hit(Item("yahoo_auction", "z1", "LP", 5200, "https://auctions.yahoo.co.jp/jp/auction/z1"), "w", "w", "drop", 6000)

        def text(blocks):
            settings = layout.normalize({"enabled": True, "blocks": blocks})
            return layout.render(notify._message_data(h, Ctx(rate=0.05, message=settings)), settings, "wecom", "markdown")
        self.assertIn("价格：¥5,200（降价，原 ¥6,000）", text(["price", "title"]))
        self.assertNotIn("（降价", text(["price", "drop"]))
        self.assertNotIn("（降价", text(["status", "price"]))
        new = Hit(Item("mercari", "m1", "LP", 5200, "https://jp.mercari.com/item/m1"), "w", "w")
        settings = layout.normalize({"enabled": True, "blocks": ["price"]})
        self.assertNotIn("降价", layout.render(notify._message_data(new, Ctx(rate=0.05, message=settings)),
                                              settings, "wecom", "markdown"))


if __name__ == "__main__":
    unittest.main()
