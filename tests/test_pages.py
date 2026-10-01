import re
import unittest

from goodsmare import pages
from goodsmare.config import ALL_SOURCES
from goodsmare.notify import Hit
from goodsmare.sources import Item


def page(**over):
    hits = [
        Hit(Item(source="mercari", id="m123", title="五条悟 <script>alert(1)</script> 缶バッジ", price=1500,
                 url="https://jp.mercari.com/item/m123",
                 image="https://static.mercdn.net/thumb/item/webp/m123_1.jpg?17", extra="无明显瑕疵 包邮",
                 created=1700000000), "w1", "五条悟吧唧"),
        Hit(Item(source="yahoo_auction", id="x9", title="夏油傑 アクスタ", price=800,
                 url="https://auctions.yahoo.co.jp/jp/auction/x9"), "w2", "夏油", "drop", 1000),
    ]
    data = pages.snapshot(hits, 0.05)
    data.update(over)
    return data


class PageTests(unittest.TestCase):
    def test_snapshot(self):
        data = page()
        first = data["items"][0]
        self.assertEqual(first["photo"], "https://static.mercdn.net/item/detail/orig/photos/m123_1.jpg?17")
        self.assertEqual(first["image"], "https://static.mercdn.net/thumb/item/jpeg/m123_1.jpg?17")
        self.assertEqual(first["created"], 1700000000)
        self.assertEqual(data["items"][1]["kind"], "drop")

    def test_render(self):
        html = pages.render(page())
        self.assertNotIn("<script>alert(1)", html, "标题要转义")
        self.assertIn("&lt;script&gt;", html)
        # 每件一个锚点，推送里的链接靠它跳到对应的那一件
        self.assertIn('id="i1"', html)
        self.assertIn('id="i2"', html)
        self.assertIn('href="https://jp.mercari.com/item/m123"', html)
        self.assertIn('data-copy="https://jp.mercari.com/item/m123"', html)
        self.assertIn("≈ 75 元", html)
        self.assertIn("无明显瑕疵 包邮", html)
        self.assertIn("降价 −20%", html)
        self.assertIn("原价 <s>¥1,000</s>，便宜了 ¥200", html)
        self.assertIn('href="/#watch=w1"', html)
        self.assertIn("上新 1 件，降价 1 件", html)
        self.assertIn('data-small="https://static.mercdn.net/thumb/item/jpeg/m123_1.jpg?17"', html,
                      "大图挂了退回缩略图")

    def test_site_colors_come_from_index(self):
        html = pages.render(page())
        for key in ALL_SOURCES:
            self.assertRegex(html, rf"--s-{key}:\s*#[0-9a-fA-F]+")

    def test_unsafe_urls_are_dropped(self):
        data = page()
        data["items"][0]["url"] = "javascript:alert(1)"
        data["items"][0]["photo"] = "javascript:alert(2)"
        html = pages.render(data)
        self.assertNotIn("javascript:alert", html)

    def test_test_push_has_no_edit_link(self):
        data = page()
        data["items"][0]["watch_id"] = "test"
        self.assertNotIn("/#watch=test", pages.render(data))

    def test_other_pages(self):
        self.assertIn("已经不在了", pages.render_missing())
        login = pages.render_login()
        self.assertIn('name="token"', login)
        self.assertNotIn("不对", login)
        self.assertIn("不对", pages.render_login(wrong=True))

    def test_ids(self):
        self.assertTrue(pages.ID_RE.match("AbCdEfGh_-123456"))
        self.assertFalse(pages.ID_RE.match("../../etc/passwd"))
        self.assertFalse(re.match(pages.ID_RE, "short"))


if __name__ == "__main__":
    unittest.main()
