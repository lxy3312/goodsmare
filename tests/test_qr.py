import hashlib
import re
import unittest

from goodsmare import qr


def fingerprint(q: qr.QR) -> str:
    bits = "".join("1" if c else "0" for row in q.modules for c in row)
    return hashlib.sha1(bits.encode()).hexdigest()


def format_bits(q: qr.QR) -> int:
    """从左上角读回 15 位格式信息（去掉固定的掩码 0x5412）。"""
    m = q.modules
    cells = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    bits = sum(1 << i for i, (x, y) in enumerate(cells) if m[y][x])
    return bits ^ 0x5412


class QRTests(unittest.TestCase):
    def test_known_codes(self):
        # 这两个的输出用 zxing-cpp 解码核对过（2026 年 9 月），之后改代码不能让它们变
        q = qr.QR("http://192.168.1.5:8787/?token=AbCdEfGhIjKl")
        self.assertEqual((q.version, q.size), (4, 33))
        self.assertEqual(fingerprint(q), "02f9057c8eb7f73f3d43b50fec8897dbae8a332c")
        q = qr.QR("x" * 150)   # 版本 7 以上多了版本信息
        self.assertEqual(q.version, 8)
        self.assertEqual(fingerprint(q), "317e2540eb3f5f3f0fe6c6a101bc0df15388558d")

    def test_version_grows_with_length(self):
        self.assertEqual(qr.QR("a").version, 1)
        self.assertEqual(qr.QR("a" * 14).version, 1)    # M 级版本 1 最多 14 字节
        self.assertEqual(qr.QR("a" * 15).version, 2)
        self.assertEqual(qr.QR("中" * 14).version, 3)   # 按 UTF-8 字节算
        with self.assertRaises(ValueError):
            qr.QR("a" * 3000)

    def test_finder_patterns_and_format(self):
        q = qr.QR("https://example.com/")
        n = q.size
        for x0, y0 in ((0, 0), (n - 7, 0), (0, n - 7)):
            ring = [q.modules[y0][x0 + i] for i in range(7)]
            self.assertEqual(ring, [True] * 7)
            self.assertFalse(q.modules[y0 + 1][x0 + 1])
            self.assertTrue(q.modules[y0 + 3][x0 + 3])
        fmt = format_bits(q)
        self.assertEqual(fmt >> 13, 0, "纠错等级是 M")
        self.assertIn((fmt >> 10) & 7, range(8))

    def test_svg_matches_modules(self):
        q = qr.QR("http://192.168.1.5:8787/?token=AbCdEfGhIjKl")
        svg = q.svg(border=3)
        drawn = set()
        for x, y, w in re.findall(r"M(\d+),(\d+)h(\d+)v1h-\d+z", svg):
            for i in range(int(w)):
                drawn.add((int(x) + i - 3, int(y) - 3))
        want = {(x, y) for y, row in enumerate(q.modules) for x, c in enumerate(row) if c}
        self.assertEqual(drawn, want)
        self.assertIn(f'viewBox="0 0 {q.size + 6} {q.size + 6}"', svg)


if __name__ == "__main__":
    unittest.main()
