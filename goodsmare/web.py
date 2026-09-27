"""本地网页：看上新、管关注、配推送。只用标准库 http.server。

默认只监听 127.0.0.1。想在手机上看就把 host 改成 0.0.0.0，并务必设一个 token。
防护：检查 Host 头（防 DNS rebinding）；写操作只收 application/json（别的网页没法跨域偷偷提交）。
"""

from __future__ import annotations

import http.cookies
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_NAME, __version__, net, notify
from .config import ALL_SOURCES, ConfigStore, normalize_channel, normalize_watch
from .monitor import Monitor, passes, query_of
from .sources import SOURCES
from .store import Store

STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


class App:
    def __init__(self, cfgstore: ConfigStore, store: Store, monitor: Monitor):
        self.cfgstore = cfgstore
        self.store = store
        self.monitor = monitor

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
        }

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
        notify.send(ch, hits, self.monitor.ctx(cfg))
        return {"message": f"已发送（{where}），去看看收到没有"}

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
        def _host_ok(self) -> bool:
            cfg_host = app.cfgstore.load()["web"]["host"]
            if cfg_host not in LOOPBACK:
                return True   # 对外开放时靠 token
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in LOOPBACK

        def _token_ok(self) -> bool:
            token = app.cfgstore.load()["web"].get("token") or ""
            if not token:
                return True
            cookie = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
            if "goodsmare_token" in cookie and secrets.compare_digest(cookie["goodsmare_token"].value, token):
                return True
            return secrets.compare_digest(self.headers.get("X-Token") or "", token)

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

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"bad host", "text/plain")
            url = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            token = app.cfgstore.load()["web"].get("token") or ""
            if token and secrets.compare_digest(q.get("token", ""), token):
                # 带着 ?token= 打开一次，之后靠 cookie
                return self._send(302, b"", "text/plain", {
                    "Location": url.path or "/",
                    "Set-Cookie": f"goodsmare_token={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"})
            if not self._token_ok():
                return self._send(401, "需要口令：在地址后面加上 ?token=你设的口令".encode(), "text/plain; charset=utf-8")
            if url.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            try:
                if url.path == "/api/state":
                    return self._json(200, app.state())
                if url.path == "/api/feed":
                    return self._json(200, app.feed(q))
            except Exception as e:
                return self._json(500, {"error": str(e)})
            self._send(404, b"not found", "text/plain")

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
                return self._json(200, getattr(app, name)(body) or {})
            except (ValueError, KeyError) as e:
                return self._json(400, {"error": str(e)})
            except net.FetchError as e:
                return self._json(502, {"error": str(e)})
            except Exception as e:
                return self._json(500, {"error": str(e) or repr(e)})

    return Handler


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    return server

