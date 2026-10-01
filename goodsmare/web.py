"""本地网页：看上新、管关注、配推送，还有推送页。只用标准库 http.server。

默认只监听 127.0.0.1。「设置 → 手机访问」打开后监听 0.0.0.0，同时必须有口令（token）。
防护：没设口令时只认本机的 Host 头（防 DNS rebinding）；设了口令就全靠口令；
写操作只收 application/json（别的网页没法跨域偷偷提交）。推送页 /p/<随机 ID> 不要口令，ID 就是钥匙。
"""

from __future__ import annotations

import hmac
import http.cookies
import json
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_NAME, __version__, access, migrate, net, notify, pages, qr
from .access import LOOPBACK
from .config import ALL_SOURCES, ConfigStore, normalize_channel, normalize_watch
from .monitor import Monitor, passes, query_of
from .sources import SOURCES
from .store import Store

STATIC = Path(__file__).parent / "static"
COOKIE = "goodsmare_token"


def _same(a: str, b: str) -> bool:
    # compare_digest 遇到非 ASCII 的 str 会抛异常，统一转成字节再比
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


class App:
    def __init__(self, cfgstore: ConfigStore, store: Store, monitor: Monitor):
        self.cfgstore = cfgstore
        self.store = store
        self.monitor = monitor
        self.server: WebServer | None = None   # WebServer 启动时填上，换监听地址要用

    # —— 读 ——
    def state(self) -> dict:
        cfg = self.cfgstore.load()
        m = self.monitor
        return {
            "app": APP_NAME, "version": __version__,
            "config": cfg,
            "sources": [{"key": k, "name": SOURCES[k].name, "home": SOURCES[k].home} for k in ALL_SOURCES],
            "channel_types": {k: {"label": v[0], "fields": [
                {"key": f[0], "label": f[1], "hint": f[2], "required": f[3]} for f in v[1]]}
                for k, v in notify.CHANNEL_TYPES.items()},
            "status": self.store.statuses(),
            "monitor": {"busy": m.busy, "last_cycle_at": int(m.last_cycle_at),
                        "next_cycle_at": int(m.next_cycle_at), "now": int(time.time())},
            "rate": m.rate(cfg, fetch=False),
            "logs": list(m.logs)[-60:],
            "access": self.access_state(cfg),
        }

    def access_state(self, cfg: dict) -> dict:
        lan = access.lan_on(cfg)
        return {"lan": lan, "listening": self.server.host if self.server else cfg["web"]["host"],
                "port": cfg["web"]["port"], "ips": access.lan_ips() if lan else [],
                "base": access.phone_base(cfg)}

    def feed(self, q: dict) -> dict:
        items = self.store.feed(after=_int(q.get("after")), before=_int(q.get("before")),
                                limit=_int(q.get("limit")) or 60, watch_id=q.get("watch", ""),
                                source=q.get("source", ""))
        return {"items": items}

    # —— 写 ——
    def save_watch(self, body: dict) -> dict:
        watch = normalize_watch(body)
        if not watch["keyword"]:
            raise ValueError("关键词不能为空")
        if not watch["sources"]:
            raise ValueError("至少选一个网站")

        def fn(cfg):
            for i, w in enumerate(cfg["watches"]):
                if w["id"] == watch["id"]:
                    cfg["watches"][i] = watch
                    break
            else:
                cfg["watches"].insert(0, watch)
        self.cfgstore.update(fn)
        self.monitor.trigger()
        return watch

    def delete_watch(self, body: dict) -> dict:
        wid = body.get("id", "")
        self.cfgstore.update(lambda cfg: cfg.__setitem__(
            "watches", [w for w in cfg["watches"] if w["id"] != wid]))
        self.store.forget_watch(wid)
        return {}

    def save_settings(self, body: dict) -> dict:
        allowed = ("interval", "request_gap", "proxy", "jpy_to_cny", "auto_rate")

        def fn(cfg):
            for k in allowed:
                if k in body:
                    cfg[k] = body[k]
        cfg = self.cfgstore.update(fn)
        net.set_proxy(cfg["proxy"])
        if "auto_rate" in body or "jpy_to_cny" in body:
            self.store.kv_set("rate_at", "0")
        return {}

    def save_channel(self, body: dict) -> dict:
        ch = normalize_channel(body)
        if ch["type"] not in notify.CHANNEL_TYPES:
            raise ValueError("不认识的推送方式")
        missing = notify.missing_fields(ch)
        if missing:
            raise ValueError(f"没填：{'、'.join(missing)}")

        def fn(cfg):
            for i, c in enumerate(cfg["channels"]):
                if c["id"] == ch["id"]:
                    cfg["channels"][i] = ch
                    break
            else:
                cfg["channels"].append(ch)
        self.cfgstore.update(fn)
        return ch

    def delete_channel(self, body: dict) -> dict:
        cid = body.get("id", "")
        self.cfgstore.update(lambda cfg: cfg.__setitem__(
            "channels", [c for c in cfg["channels"] if c["id"] != cid]))
        return {}

    def test_channel(self, body: dict) -> dict:
        cfg = self.cfgstore.load()
        ch = normalize_channel(body) if body.get("type") else next(
            (c for c in cfg["channels"] if c["id"] == body.get("id")), None)
        if ch is None:
            raise ValueError("找不到这个推送渠道")
        net.set_proxy(cfg["proxy"])
        hits, where = self.monitor.test_hits(cfg)
        ctx = self.monitor.ctx(cfg, hits)
        notify.send(ch, hits, ctx)
        page = "，点开应该是推送页" if ctx.page_url else ""
        return {"message": f"已发送（{where}{page}），去看看收到没有"}

    def save_access(self, body: dict) -> dict:
        """「手机访问」：局域网开关、手机用的地址、推送点开去哪。对外开放了就必须有口令。"""
        before = self.cfgstore.load()["web"]["host"]

        def fn(cfg):
            web = cfg["web"]
            if "lan" in body:
                web["host"] = access.LAN_HOST if body["lan"] else "127.0.0.1"
            if "public_url" in body:
                web["public_url"] = access.normalize_public_url(str(body["public_url"] or ""))
            if "push_page" in body:
                web["push_page"] = bool(body["push_page"])
            if (access.lan_on(cfg) or web["public_url"]) and not web["token"]:
                web["token"] = access.new_token()
        cfg = self.cfgstore.update(fn)
        host = cfg["web"]["host"]
        if self.server is not None and host != self.server.host:
            try:
                self.server.rebind(host)
            except OSError as e:
                self.cfgstore.update(lambda c: c["web"].update({"host": before}))
                raise ValueError(f"没能对局域网开放：{e}") from e
        token = cfg["web"]["token"]
        # 刚生成口令时，这个浏览器也得拿到它，不然下一次刷新就被拦在外面
        return {"_cookie": token} if token else {}

    def migrate(self, body: dict) -> dict:
        """从旧版迁移。apply 为假时只说会怎么合并；真合并时先停下扫描，用正在用的连接写。"""
        path = str(body.get("path") or "").strip().strip('"').strip()
        if not path:
            raise ValueError("先填旧版的文件夹")
        data_dir = self.cfgstore.path.parent
        if not body.get("apply"):
            text, changes = migrate.run(path, data_dir, store=self.store, cfgstore=self.cfgstore)
            return {"report": text, "changes": changes}
        if not self.monitor._cycle_lock.acquire(timeout=90):
            raise ValueError("正在扫描，过一会儿再试")
        try:
            text, _ = migrate.run(path, data_dir, apply=True, store=self.store, cfgstore=self.cfgstore)
        finally:
            self.monitor._cycle_lock.release()
        return {"report": text, "applied": True}

    def preview(self, body: dict) -> dict:
        """新建关注前先搜一下看看：直接返回搜索结果，不入库。"""
        watch = normalize_watch(body)
        if not watch["keyword"]:
            raise ValueError("先填关键词")
        key = body.get("source") or (watch["sources"][0] if watch["sources"] else "mercari")
        src = SOURCES.get(key)
        if src is None:
            raise ValueError("不认识的网站")
        net.set_proxy(self.cfgstore.load()["proxy"])
        with src.lock:
            items = src.search(query_of(watch))
        return {"items": [dict(it.to_dict(), passes=passes(watch, it)) for it in items[:60]],
                "total": len(items)}

    def scan(self, body: dict) -> dict:
        self.monitor.trigger()
        return {}

    def clear_feed(self, body: dict) -> dict:
        self.store.clear_feed()
        return {}


POSTS = {
    "/api/watch": "save_watch", "/api/watch/delete": "delete_watch",
    "/api/settings": "save_settings",
    "/api/channel": "save_channel", "/api/channel/delete": "delete_channel",
    "/api/channel/test": "test_channel",
    "/api/preview": "preview", "/api/scan": "scan", "/api/feed/clear": "clear_feed",
    "/api/access": "save_access", "/api/migrate": "migrate",
}


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "goodsmare/" + __version__

        def log_message(self, fmt, *args):  # 安静点
            pass

        # —— 安全检查 ——
        def _token(self) -> str:
            return app.cfgstore.load()["web"].get("token") or ""

        def _host_ok(self) -> bool:
            if self._token():
                return True   # 有口令就靠口令，手机、内网穿透的地址都放行
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in LOOPBACK

        def _token_ok(self) -> bool:
            token = self._token()
            if not token:
                return True
            try:
                cookie = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
            except http.cookies.CookieError:
                cookie = {}
            if COOKIE in cookie and _same(cookie[COOKIE].value, token):
                return True
            return _same(self.headers.get("X-Token") or "", token)

        def _cookie(self, token: str) -> str:
            return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj, extra: dict | None = None):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def _html(self, code: int, text: str):
            self._send(code, text.encode("utf-8"), "text/html; charset=utf-8")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, "这个地址现在打不开：先在电脑上的 goodsmare 里打开「设置 → 手机访问」。"
                                  .encode(), "text/plain; charset=utf-8")
            url = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            if url.path.startswith("/p/"):
                return self._push_page(url.path[3:])
            token = self._token()
            if token and _same(q.get("token", ""), token):
                # 带着 ?token= 打开一次（扫二维码就是这样），之后靠 cookie
                return self._send(302, b"", "text/plain", {"Location": url.path or "/", "Set-Cookie": self._cookie(token)})
            if not self._token_ok():
                if url.path in ("/", "/index.html"):
                    return self._html(401, pages.render_login(wrong="token" in q))
                return self._json(401, {"error": "需要口令"})
            if url.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            try:
                if url.path == "/api/state":
                    return self._json(200, app.state())
                if url.path == "/api/feed":
                    return self._json(200, app.feed(q))
                if url.path == "/api/qr.svg":
                    return self._send(200, qr.svg(q.get("text", "")[:1000]).encode(), "image/svg+xml")
            except Exception as e:
                return self._json(500, {"error": str(e)})
            self._send(404, b"not found", "text/plain")

        def _push_page(self, pid: str):
            page = app.store.page(pid) if pages.ID_RE.match(pid) else None
            if page is None:
                return self._html(404, pages.render_missing())
            return self._html(200, pages.render(page))

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, b"bad host", "text/plain")
            if not self._token_ok():
                return self._json(401, {"error": "需要口令"})
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(415, {"error": "只接受 JSON"})
            name = POSTS.get(urllib.parse.urlsplit(self.path).path)
            if name is None:
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    raise ValueError("请求格式不对")
                result = getattr(app, name)(body) or {}
                token = result.pop("_cookie", None)
                return self._json(200, result, {"Set-Cookie": self._cookie(token)} if token else None)
            except (ValueError, KeyError) as e:
                return self._json(400, {"error": str(e)})
            except net.FetchError as e:
                return self._json(502, {"error": str(e)})
            except Exception as e:
                return self._json(500, {"error": str(e) or repr(e)})

    return Handler


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False   # 换监听地址是在请求线程里做的，关旧服务时别等这些线程

    def server_bind(self):
        # http.server 原本在这里做一次反向 DNS（getfqdn），有的 Windows 上要卡好几秒，用不上
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]


class WebServer:
    """网页服务。「手机访问」一拨，监听地址在 127.0.0.1 和 0.0.0.0 之间切换，不用重启。"""

    def __init__(self, app: App, host: str, port: int):
        self.app = app
        self._lock = threading.Lock()
        self.httpd = self._start(host, port)
        self.host = host
        self.port = self.httpd.server_address[1]
        app.server = self

    def _start(self, host: str, port: int) -> _HTTPServer:
        httpd = _HTTPServer((host, port), make_handler(self.app))
        threading.Thread(target=httpd.serve_forever, daemon=True, name="web").start()
        return httpd

    @property
    def server_address(self):
        return self.httpd.server_address

    def rebind(self, host: str) -> None:
        with self._lock:
            if host == self.host:
                return
            old = self.httpd
            old.shutdown()
            old.server_close()
            try:
                self.httpd = self._start(host, self.port)
            except OSError:
                self.httpd = self._start(self.host, self.port)   # 换不过去就回到原来的地址
                raise
            self.host = host

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def serve(app: App, host: str, port: int) -> WebServer:
    return WebServer(app, host, port)

