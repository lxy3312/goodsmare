"""可复用的 HTTP/1.1 连接；仍交给 urllib 处理代理、跳转和 Cookie。"""

from __future__ import annotations

import atexit
import collections
import contextlib
import hashlib
import http.client
import io
import select
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.response

IDLE_TTL = 30
MAX_IDLE = 32
MAX_PER_ORIGIN = 4


class ConnectionPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._idle = collections.OrderedDict()
        self._generation = 0

    def take(self, key, fresh: bool = False):
        """拿一条空闲连接；fresh 为真时不拿旧的，只返回当前代数（新连接用完照样能放回来）。"""
        now = time.monotonic()
        with self._lock:
            if fresh:
                return None, self._generation
            for old_key in list(self._idle):
                live = []
                for connection, at in self._idle[old_key]:
                    if now - at < IDLE_TTL:
                        live.append((connection, at))
                    else:
                        connection.close()
                if live:
                    self._idle[old_key] = live
                else:
                    self._idle.pop(old_key)
            entries = self._idle.pop(key, [])
            while entries:
                connection, _ = entries.pop()
                try:
                    # 空闲连接可读通常说明对端已关连接；发送前丢弃，避免拿旧连接碰运气。
                    usable = connection.sock is not None and not select.select([connection.sock], [], [], 0)[0]
                except (OSError, ValueError):
                    usable = False
                if usable:
                    if entries:
                        self._idle[key] = entries
                    return connection, self._generation
                connection.close()
            return None, self._generation

    def put(self, key, connection, generation):
        with self._lock:
            if generation != self._generation:
                connection.close()
                return
            entries = self._idle.setdefault(key, [])
            if len(entries) >= MAX_PER_ORIGIN:
                connection.close()
                return
            entries.append((connection, time.monotonic()))
            self._idle.move_to_end(key)
            while sum(len(v) for v in self._idle.values()) > MAX_IDLE:
                _, old = self._idle.popitem(last=False)
                for conn, _ in old:
                    conn.close()

    def clear(self):
        with self._lock:
            self._generation += 1
            for entries in self._idle.values():
                for connection, _ in entries:
                    connection.close()
            self._idle.clear()


POOL = ConnectionPool()
atexit.register(POOL.clear)
_tls = threading.local()


@contextlib.contextmanager
def fresh_connection(on: bool = True):
    """这一段里的请求不用池子里的旧连接：旧连接刚被对方关掉、要重发时用。"""
    previous = getattr(_tls, "fresh", False)
    _tls.fresh = on
    try:
        yield
    finally:
        _tls.fresh = previous


def died_while_idle(error: BaseException) -> bool:
    """复用的旧连接还没收到任何回应就被对方断开：多半是对方刚好关掉了闲置连接，请求没有被处理，可以重发。"""
    reason = getattr(error, "reason", error)
    return bool(getattr(error, "reused_connection", False)) and isinstance(
        reason, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, ConnectionAbortedError))
_context = None
_context_lock = threading.Lock()


def tls_context():
    global _context
    with _context_lock:
        if _context is None:
            _context = ssl.create_default_context()
            _context.set_alpn_protocols(["http/1.1"])
        return _context


class _PooledOpen:
    def __init__(self, options, session=None):
        self.options = options
        self.session = session

    def open(self, req, secure):
        started = time.perf_counter()
        headers = dict(req.unredirected_hdrs)
        headers.update({k: v for k, v in req.headers.items() if k not in headers})
        headers = {k.title(): v for k, v in headers.items()}
        tunnel = getattr(req, "_tunnel_host", None)
        auth = headers.get("Proxy-Authorization", "")
        origin = urllib.parse.urlsplit(req.full_url).netloc
        key = (secure, req.host, tunnel, origin, hashlib.sha256(auth.encode()).digest(), self.session)
        keepalive = self.options["keepalive"] and headers.get("Connection", "").lower() != "close"
        conn, generation = POOL.take(key, getattr(_tls, "fresh", False)) if keepalive else (None, -1)
        reused = conn is not None
        connect_timeout = min(float(req.timeout), self.options["connect_timeout"])
        read_timeout = min(float(req.timeout), self.options["read_timeout"])
        if conn is None:
            cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
            kw = {"context": tls_context()} if secure else {}
            conn = cls(req.host, timeout=connect_timeout, **kw)
            if tunnel:
                conn.set_tunnel(tunnel, headers={"Proxy-Authorization": auth} if auth else {})
        if tunnel or not req.has_proxy():
            headers.pop("Proxy-Authorization", None)
        headers["Connection"] = "keep-alive" if keepalive else "close"
        try:
            if conn.sock is None:
                conn.connect()
            connected = time.perf_counter()
            conn.timeout = read_timeout
            conn.sock.settimeout(read_timeout)
            conn.request(req.get_method(), req.selector, req.data, headers,
                         encode_chunked=req.has_header("Transfer-encoding"))
            response = conn.getresponse()
            received = time.perf_counter()
            body = response.read()  # 完整消费后才能把连接归还；跳转和 HTTP 错误也如此。
            finished = time.perf_counter()
            reusable = keepalive and not response.will_close and conn.sock is not None
            result = urllib.response.addinfourl(io.BytesIO(body), response.headers, req.full_url, response.status)
            result.msg = response.reason
            result.network_timing = {"reused": reused, "connect": connected - started,
                                     "response": received - connected, "download": finished - received,
                                     "total": finished - started, "bytes": len(body),
                                     "proxy": req.host if tunnel or req.has_proxy() else ""}
            response.close()
            if reusable:
                POOL.put(key, conn, generation)
            else:
                conn.close()
            return result
        except (OSError, http.client.HTTPException) as error:
            conn.close()
            wrapped = urllib.error.URLError(error)
            wrapped.reused_connection = reused
            wrapped.network_timing = {"reused": reused, "proxy": req.host if tunnel or req.has_proxy() else ""}
            raise wrapped from error
        except BaseException:
            conn.close()
            raise


class HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, options, session=None):
        super().__init__()
        self._pooled = _PooledOpen(options, session)

    def http_open(self, req):
        return self._pooled.open(req, False)


class HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, options, session=None):
        super().__init__(context=tls_context())
        self._pooled = _PooledOpen(options, session)

    def https_open(self, req):
        return self._pooled.open(req, True)
