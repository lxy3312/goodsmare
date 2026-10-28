import http.cookiejar
import socket
import socketserver
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from goodsmare import net


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeProxy:
    """只会回一句话的「代理」：收到请求就回 reply。"""

    def __init__(self, reply: bytes):
        seen = self.seen = []

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                seen.append(self.request.recv(1024))
                self.request.sendall(reply)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CookieGate(BaseHTTPRequestHandler):
    """学 Mandarake：没 cookie 就 302 回首页并发 cookie，有 cookie 才给列表。"""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/list"):
            if "gate=1" in (self.headers.get("Cookie") or ""):
                body = b"list page"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header("Location", "/home")
            self.send_header("Set-Cookie", "gate=1; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"home page"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NetTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CookieGate)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # 本机地址别走系统代理
        patch = mock.patch.object(net, "_proxies", lambda: {})
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_cookie_jar_is_kept_between_requests(self):
        jar = http.cookiejar.CookieJar()
        first = net.get(self.base + "/list", cookies=jar)
        self.assertTrue(first.url.endswith("/home"))
        second = net.get(self.base + "/list", cookies=jar)
        self.assertEqual(second.body, b"list page")

    def test_without_jar_nothing_is_remembered(self):
        for _ in range(2):
            self.assertEqual(net.get(self.base + "/list").body, b"home page")


    def test_system_proxy_is_reread(self):
        """Clash 的「系统代理」开关拨来拨去，程序不用重启就能跟上。"""
        found = [{}]
        try:
            with mock.patch.object(net, "SYS_TTL", 0), \
                    mock.patch.object(net.urllib.request, "getproxies", lambda: found[0]):
                net.set_proxy("")
                self.assertEqual(net.proxy_info(), {"mode": "direct", "proxy": ""})
                found[0] = {"https": "http://127.0.0.1:7890", "http": "http://127.0.0.1:7890", "no": "localhost"}
                self.assertEqual(net.proxy_info(), {"mode": "system", "proxy": "http://127.0.0.1:7890"})
                self.assertEqual(set(net.system_proxies()), {"http", "https"})
                net.set_proxy("127.0.0.1:7897")
                self.assertEqual(net.proxy_info(), {"mode": "app", "proxy": "http://127.0.0.1:7897"})
        finally:
            net.set_proxy("")
            net._sys_cache = None

    def test_unreachable(self):
        port = closed_port()
        with self.assertRaises(net.Unreachable) as cm:
            net.get(f"http://127.0.0.1:{port}/", retries=0, timeout=8)
        self.assertEqual((cm.exception.kind, cm.exception.host), ("net", "127.0.0.1"))
        self.assertIn(f"连不上 127.0.0.1:{port}（连接被拒绝）", str(cm.exception))

    def test_dead_proxy_is_named(self):
        dead = f"http://127.0.0.1:{closed_port()}"
        with mock.patch.object(net, "_proxies", lambda: {"http": dead, "https": dead}):
            with self.assertRaises(net.Unreachable) as cm:
                net.get("http://example.invalid/", retries=0, timeout=8)
        self.assertIn(f"代理 {dead} 连不上", str(cm.exception))

    def test_find_local_proxy(self):
        good, bad = FakeProxy(b"HTTP/1.1 200 Connection established\r\n\r\n"), FakeProxy(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        try:
            with mock.patch.object(net, "LOCAL_PROXY_PORTS", (closed_port(), bad.port, good.port)):
                self.assertEqual(net.find_local_proxy("api.mercari.jp"), f"http://127.0.0.1:{good.port}")
            self.assertEqual(good.seen[0].split(b"\r\n")[0], b"CONNECT api.mercari.jp:443 HTTP/1.1")
            with mock.patch.object(net, "LOCAL_PROXY_PORTS", (bad.port,)):
                self.assertEqual(net.find_local_proxy("api.mercari.jp"), "")
        finally:
            good.close()
            bad.close()


if __name__ == "__main__":
    unittest.main()
