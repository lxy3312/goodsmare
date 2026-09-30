import http.cookiejar
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from goodsmare import net


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
        direct = [urllib.request.ProxyHandler({})]
        for patch in (mock.patch.object(net, "_proxy_handlers", direct),
                      mock.patch.object(net, "_opener", urllib.request.build_opener(*direct))):
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


if __name__ == "__main__":
    unittest.main()
