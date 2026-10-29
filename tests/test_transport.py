import concurrent.futures
import gzip
import http.cookiejar
import json
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from goodsmare import net, transport
from goodsmare.config import ConfigStore, normalize_network
from goodsmare.monitor import Monitor
from goodsmare.store import Store
from goodsmare.web import App


class CountingServer(ThreadingHTTPServer):
    def __init__(self, handler):
        super().__init__(("127.0.0.1", 0), handler)
        self.accepted = 0
        self.posts = 0
        self.tunnels = 0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def get_request(self):
        connection, address = super().get_request()
        self.accepted += 1
        return connection, address

    def handle_error(self, *args):
        pass  # 断线和不受信的证书属于测试场景

    def close(self):
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path
        body = path.encode()
        status, headers = 200, {}
        if path == "/redirect":
            status, body = 302, b""
            headers = {"Location": "/cookie", "Set-Cookie": "gate=1; Path=/"}
        elif path == "/cookie":
            body = self.headers.get("Cookie", "").encode()
        elif path == "/gzip":
            body = gzip.compress(b'{"value":42}')
            headers["Content-Encoding"] = "gzip"
        elif path == "/close":
            headers["Connection"] = "close"
        elif path == "/error":
            status = 503
            headers["Retry-After"] = "120"
        elif path == "/cut":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"short")
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        if path == "/stale":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True

    def do_POST(self):
        self.server.posts += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.connection.shutdown(socket.SHUT_RDWR)
        self.close_connection = True

    def do_CONNECT(self):
        self.server.tunnels += 1
        host, port = self.path.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=3) as upstream:
            self.send_response(200)
            self.end_headers()
            self.wfile.flush()
            while True:
                ready, _, _ = select.select([upstream, self.connection], [], [], 3)
                if not ready:
                    break
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        self.close_connection = True
                        return
                    destination = self.connection if source is upstream else upstream
                    destination.sendall(data)
        self.close_connection = True


class TransportTests(unittest.TestCase):
    def setUp(self):
        net.configure({})
        net.reset_connections()
        self.server = CountingServer(Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.patch = mock.patch("goodsmare.net._proxies", return_value={})
        self.patch.start()

    def tearDown(self):
        transport.POOL.clear()
        self.server.close()
        self.patch.stop()
        net.configure({})
        net.reset_connections()

    def test_connections_are_reused_and_can_be_disabled(self):
        first = net.get(self.url + "/one", retries=0)
        second = net.get(self.url + "/two", retries=0)
        self.assertEqual((first.body, second.body), (b"/one", b"/two"))
        self.assertFalse(first.network_timing["reused"])
        self.assertTrue(second.network_timing["reused"])
        self.assertEqual(self.server.accepted, 1)
        net.configure({"network": {"keepalive": False}})
        for _ in range(2):
            net.get(self.url + "/off", retries=0)
        self.assertEqual(self.server.accepted, 3)

    def test_redirect_cookies_remain_isolated_and_gzip_is_decoded(self):
        jar = http.cookiejar.CookieJar()
        result = net.get(self.url + "/redirect", cookies=jar, retries=0)
        self.assertEqual(result.body, b"gate=1")
        self.assertTrue(result.url.endswith("/cookie"))
        self.assertEqual(net.get(self.url + "/cookie", retries=0).body, b"")
        self.assertEqual(net.get(self.url + "/gzip", retries=0).json(), {"value": 42})

    def test_configured_timeouts_and_explicit_cap_reach_socket(self):
        net.configure({"network": {"connect_timeout": 25, "read_timeout": 41}})
        net.get(self.url + "/ok", retries=0)
        connection = next(iter(transport.POOL._idle.values()))[0][0]
        self.assertEqual(connection.sock.gettimeout(), 41)
        net.get(self.url + "/ok", retries=0, timeout=7)
        self.assertEqual(connection.sock.gettimeout(), 7)
        net.configure({"network": {"routes": {"mercari": "direct"}}})
        self.assertEqual(net.network_info()["groups"], {}, "切换设置后不能混入旧线路的统计")

    def test_failed_proxy_is_reported_without_credentials(self):
        self.server.close()
        proxy = f"http://user:test-password@127.0.0.1:{self.server.server_port}"
        net.configure({"network": {"routes": {"mercari": proxy}}})
        with mock.patch("urllib.request.proxy_bypass", return_value=False), net.route("mercari"):
            with self.assertRaises(net.Unreachable):
                net.get("http://example.invalid/test", retries=0, timeout=2)
        info = net.network_info()["groups"]["mercari"]
        self.assertIn("127.0.0.1", info["proxy"])
        self.assertEqual(info["errors"], 1)
        self.assertNotIn("test-password", json.dumps(info))

    def test_closed_and_incomplete_responses_do_not_poison_pool(self):
        for path in ("/close", "/stale"):
            net.get(self.url + path, retries=0)
            before = self.server.accepted
            self.assertEqual(net.get(self.url + "/ok").body, b"/ok")
            self.assertGreater(self.server.accepted, before)
        with self.assertRaises(net.Unreachable):
            net.get(self.url + "/cut", retries=0)
        self.assertEqual(net.get(self.url + "/ok", retries=0).body, b"/ok")

    def test_http_errors_keep_headers_and_reusable_connection(self):
        error = net.get(self.url + "/error")
        self.assertEqual(error.status, 503)
        self.assertEqual(error.retry_after(), 120)
        self.assertEqual(error.network_timing["attempts"], 1)
        self.assertTrue(net.get(self.url + "/ok").network_timing["reused"])

    def test_post_is_not_silently_duplicated_after_lost_response(self):
        with self.assertRaises(net.Unreachable):
            net.post(self.url + "/post", json_body={"message": "test"})
        self.assertEqual(self.server.posts, 1)
        with mock.patch("goodsmare.net.time.sleep"), self.assertRaises(net.Unreachable):
            net.post(self.url + "/search", json_body={"query": "test"}, retries=1)
        self.assertEqual(self.server.posts, 3, "只有显式允许重试的只读 POST 才重试")

    def test_concurrent_requests_do_not_mix_responses(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda n: net.get(self.url + f"/{n}", retries=0).body, range(18)))
        self.assertEqual(responses, [f"/{n}".encode() for n in range(18)])

    def test_site_routes_and_domestic_push_are_independent(self):
        proxy = f"http://user:test-password@127.0.0.1:{self.server.server_port}"
        net.configure({"network": {"routes": {"mercari": proxy}}})
        with mock.patch("urllib.request.proxy_bypass", return_value=False), net.route("mercari"):
            result = net.get("http://example.invalid/test", retries=0)
        self.assertEqual(result.body, b"http://example.invalid/test")
        self.assertEqual(net._route_proxies("domestic_push", False), {})
        self.assertEqual(net._route_proxies("xianyu", False), {})
        self.assertNotIn("test-password", json.dumps(net.network_info()))
        self.assertNotIn("user:", json.dumps(net.network_info()))
        self.assertEqual(net.request_group("https://wxpusher.zjiecode.com/api/send"), "domestic_push")
        self.assertEqual(net.request_group("https://wxpusher.zjiecode.com.evil.invalid/"), "other")

    def test_reset_invalidates_connections_borrowed_before_reset(self):
        pool = transport.ConnectionPool()
        _, generation = pool.take("key")
        pool.clear()
        connection = mock.Mock()
        pool.put("key", connection, generation)
        connection.close.assert_called_once()
        self.assertIsNone(pool.take("key")[0])


OPENSSL = shutil.which("openssl") or ("C:/Program Files/Git/usr/bin/openssl.exe" if Path("C:/Program Files/Git/usr/bin/openssl.exe").exists() else None)


@unittest.skipUnless(OPENSSL, "HTTPS 集成测试需要 openssl 生成临时证书")
class SecureTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.cert, cls.key = root / "cert.pem", root / "key.pem"
        config = root / "openssl.cnf"
        config.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
        subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-config", str(config),
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                        "-keyout", str(cls.key), "-out", str(cls.cert)],
                       capture_output=True, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_https_proxy_tunnel_reuse_and_certificate_verification(self):
        net.configure({})
        transport.POOL.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.cert), str(self.key))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        proxy = CountingServer(Handler)
        url = f"https://localhost:{server.server_port}"
        trusted = ssl.create_default_context(cafile=str(self.cert))
        try:
            with mock.patch("goodsmare.net._proxies", return_value={}):
                with self.assertRaises(net.Unreachable):
                    net.get(url + "/untrusted", retries=0, timeout=3)
            with mock.patch("goodsmare.transport.tls_context", return_value=trusted), \
                    mock.patch("urllib.request.proxy_bypass", return_value=False):
                net.configure({"network": {"routes": {"mercari": f"http://127.0.0.1:{proxy.server_port}"}}})
                with net.route("mercari"):
                    first = net.get(url + "/one", retries=0)
                    second = net.get(url + "/two", retries=0)
                self.assertEqual((first.body, second.body), (b"/one", b"/two"))
                self.assertTrue(second.network_timing["reused"])
                self.assertEqual(proxy.tunnels, 1)
                self.assertTrue(trusted.check_hostname)
        finally:
            transport.POOL.clear()
            proxy.close()
            server.shutdown()
            server.server_close()
            net.configure({})


class NetworkSettingsTests(unittest.TestCase):
    def test_routes_are_validated_and_partial_updates_keep_other_routes(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = ConfigStore(Path(root) / "config.json")
            cfg.save({"auto_rate": False})
            store = Store(":memory:")
            monitor = Monitor(cfg, store, echo=False)
            app = App(cfg, store, monitor)
            try:
                app.save_settings({"network": {"routes": {"mercari": "127.0.0.1:7890"}}})
                app.save_settings({"network": {"keepalive": False}})
                actual = cfg.load()["network"]
                self.assertEqual(actual["routes"]["mercari"], "http://127.0.0.1:7890")
                self.assertFalse(actual["keepalive"])
                for bad in ("socks5://127.0.0.1:1080", "http://missing-port", "http://host:99/path"):
                    with self.assertRaises(ValueError):
                        app.save_settings({"network": {"routes": {"mercari": bad}}})
                app.reset_network({})
                self.assertEqual(net.network_info()["groups"], {})
            finally:
                store.db.close()
                net.configure({})
        self.assertEqual(normalize_network({"routes": {"xianyu": "http://127.0.0.1:99"}})["routes"]["xianyu"], "direct")
