import unittest
from unittest import mock

from goodsmare import access
from goodsmare.config import normalize


def cfg(**web):
    return normalize({"web": web})


class AccessTests(unittest.TestCase):
    def setUp(self):
        access._cache = (0.0, [])

    def tearDown(self):
        access._cache = (0.0, [])

    def test_kinds(self):
        self.assertEqual(access._kind("192.168.31.222"), "局域网")
        self.assertEqual(access._kind("10.0.0.8"), "局域网")
        self.assertEqual(access._kind("100.101.102.103"), "Tailscale 等")
        self.assertEqual(access._kind("198.18.0.1"), "", "Clash TUN 的假 IP")
        self.assertEqual(access._kind("26.76.48.187"), "", "Radmin VPN")
        self.assertEqual(access._kind("169.254.1.1"), "")
        self.assertEqual(access._kind("127.0.0.1"), "")

    def test_lan_ips_order(self):
        with mock.patch("socket.getaddrinfo", return_value=[
                (2, 1, 0, "", ("26.76.48.187", 0)), (2, 1, 0, "", ("100.88.1.2", 0)),
                (2, 1, 0, "", ("192.168.56.1", 0)), (2, 1, 0, "", ("192.168.31.222", 0))]), \
                mock.patch("socket.socket", side_effect=OSError):
            ips = access.lan_ips()
        self.assertEqual([i["ip"] for i in ips], ["192.168.31.222", "100.88.1.2", "192.168.56.1"])

    def test_phone_base(self):
        self.assertEqual(access.phone_base(cfg()), "", "只听本机时手机连不上")
        with mock.patch.object(access, "lan_ips", return_value=[{"ip": "192.168.1.5", "label": "局域网"}]):
            self.assertEqual(access.phone_base(cfg(host="0.0.0.0")), "http://192.168.1.5:8787")
            self.assertEqual(access.phone_base(cfg(host="0.0.0.0", public_url="https://a.example/")),
                             "https://a.example")
            self.assertEqual(access.phone_base(cfg(public_url="http://100.1.2.3:8787")), "http://100.1.2.3:8787",
                             "内网穿透可以只听本机")
        self.assertEqual(access.phone_base(cfg(host="192.168.1.9", port=9000)), "http://192.168.1.9:9000")
        with mock.patch.object(access, "lan_ips", return_value=[]):
            self.assertEqual(access.phone_base(cfg(host="0.0.0.0")), "")

    def test_normalize_public_url(self):
        n = access.normalize_public_url
        self.assertEqual(n(" 192.168.1.5:8787/ "), "http://192.168.1.5:8787")
        self.assertEqual(n("HTTPS://goods.example.com"), "https://goods.example.com")
        self.assertEqual(n(""), "")
        for bad in ("ftp://x", "http://", "http://a b"):
            with self.assertRaises(ValueError):
                n(bad)


if __name__ == "__main__":
    unittest.main()
