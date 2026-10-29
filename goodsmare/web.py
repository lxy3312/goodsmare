"""本地网页：看上新、管关注、配推送，还有推送页。只用标准库 http.server。

默认只监听 127.0.0.1。「设置 → 手机访问」打开后监听 0.0.0.0，同时必须有口令（token）。
防护：没设口令时只认本机的 Host 头（防 DNS rebinding）；设了口令就全靠口令；
写操作只收 application/json（别的网页没法跨域偷偷提交）。推送页 /p/<随机 ID> 不要口令，ID 就是钥匙。
"""

from __future__ import annotations

import copy
import hmac
import http.cookies
import json
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_NAME, __version__, access, message_layout, migrate, net, notify, pages, qr
from .access import LOOPBACK
from .config import ALL_SOURCES, ConfigStore, normalize_channel, normalize_watch
from .monitor import Monitor, estimate_period, norm, passes, query_of
from .sources import SOURCES, Item, Query
from .sources.xianyu import KV_COOKIES, account as xianyu_account
from .store import Store

STATIC = Path(__file__).parent / "static"
COOKIE = "goodsmare_token"


def _same(a: str, b: str) -> bool:
    # compare_digest 遇到非 ASCII 的 str 会抛异常，统一转成字节再比
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


# 0.3 及以前记下的状态没有 kind，看字猜一下
_GUESS = (("连不上", "net"), ("代理", "net"), ("拦下", "blocked"), ("太频繁", "blocked"),
          ("解析到 0 件", "parse"), ("登录", "login"))


def problems(cfg: dict, statuses: dict) -> list[dict]:
    """启用的关注里，最近一次没搜成的网站。同一个站、同一类问题并成一条，网页上集中说一次，
    不用在每个关注里重复一遍。all 为真表示用这个站的关注全都这样。"""
    out: dict = {}
    users: dict = {}
    for w in cfg["watches"]:
        if not w["enabled"] or not w["keyword"]:
            continue
        for key in w["sources"]:
            users[key] = users.get(key, 0) + 1
            st = statuses.get(w["id"], {}).get(key)
            if not st or st["ok"]:
                continue
            kind = st.get("kind") or next((k for word, k in _GUESS if word in st["message"]), "error")
            p = out.setdefault((key, kind), {"source": key, "kind": kind, "message": st["message"], "watches": []})
            p["watches"].append(w["name"])
    for p in out.values():
        p["all"] = len(p["watches"]) == users.get(p["source"], 0)
    return list(out.values())


class App:
    def __init__(self, cfgstore: ConfigStore, store: Store, monitor: Monitor):
        self.cfgstore = cfgstore
        self.store = store
        self.monitor = monitor
        self.server: WebServer | None = None   # WebServer 启动时填上，换监听地址要用

    # —— 读 ——
    def state(self) -> dict:
        cfg = self.cfgstore.load()
        net.configure(cfg)   # 刚启动、第一轮还没跑时，下面的「现在怎么连」也要准
        m = self.monitor
        statuses = self.store.statuses()
        return {
            "app": APP_NAME, "version": __version__,
            "config": cfg,
            "message_layout": message_layout.schema(),
            "sources": [{"key": k, "name": SOURCES[k].name, "home": SOURCES[k].home, "currency": SOURCES[k].currency,
                         "login": SOURCES[k].login, "ready": SOURCES[k].ready()} for k in ALL_SOURCES],
            # 关注页上点网站名，直接去那个网站看同一个搜索
            "links": {w["id"]: {k: SOURCES[k].search_url(query_of(w)) for k in w["sources"] if k in SOURCES}
                      for w in cfg["watches"] if w["keyword"]},
            "channel_types": {k: {"label": v[0], "fields": [
                {"key": f[0], "label": f[1], "hint": f[2], "required": f[3], "kind": f[4] if len(f) > 4 else "text",
                 "default": f[5] if len(f) > 5 else True}
                for f in v[1]], "guide": ({"url": notify.CHANNEL_GUIDES[k][0], "text": notify.CHANNEL_GUIDES[k][1]}
                                         if k in notify.CHANNEL_GUIDES else None)}
                for k, v in notify.CHANNEL_TYPES.items()},
            "status": statuses,
            "problems": problems(cfg, statuses),
            "stats": self.store.watch_stats(),
            "net": {**net.proxy_info(), "local": m.proxy_hint, "performance": net.network_info()},
            "xianyu": xianyu_account.state(),
            "monitor": {"busy": m.busy, "live": m.live, "period": m.period or estimate_period(cfg),
                        "last_check_at": int(m.last_check_at), "last_cycle_at": int(m.last_cycle_at),
                        "next_cycle_at": int(m.next_cycle_at), "now": int(time.time()),
                        "timing": m.timing_snapshot()},
            "rate": m.rate(cfg, fetch=False),
            "logs": m.log_snapshot(),
            "blocked": self.store.blocked_count(),
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

        previous = []

        def fn(cfg):
            for i, w in enumerate(cfg["watches"]):
                if w["id"] == watch["id"]:
                    previous.append(w)
                    cfg["watches"][i] = watch
                    break
            else:
                cfg["watches"].insert(0, watch)
        self.cfgstore.update(fn)
        search_fields = ("keyword", "sources", "price_min", "price_max", "exclude", "must", "enabled")
        if not previous or any(previous[0][k] != watch[k] for k in search_fields):
            self.monitor.trigger(watch["id"])
        else:
            self.monitor.wake()
        return watch

    def delete_watch(self, body: dict) -> dict:
        wid = body.get("id", "")
        self.cfgstore.update(lambda cfg: cfg.__setitem__(
            "watches", [w for w in cfg["watches"] if w["id"] != wid]))
        self.store.forget_watch(wid)
        self.monitor.wake()
        return {}

    def save_settings(self, body: dict) -> dict:
        allowed = ("interval", "request_gap", "proxy", "jpy_to_cny", "auto_rate", "buy", "push_mode")

        def fn(cfg):
            for k in allowed:
                if k in body:
                    cfg[k] = body[k]
            if "network" in body:
                if not isinstance(body["network"], dict):
                    raise ValueError("网络设置格式不正确")
                incoming = body["network"].get("routes", {})
                if not isinstance(incoming, dict):
                    raise ValueError("网站线路设置格式不正确")
                routes = {**cfg["network"]["routes"], **incoming}
                cfg["network"] = {**cfg["network"], **body["network"], "routes": routes}
        cfg = self.cfgstore.update(fn)
        net.configure(cfg)
        if "proxy" in body:
            self.monitor.proxy_hint = ""
        if "auto_rate" in body or "jpy_to_cny" in body:
            self.store.kv_set("rate_at", "0")
        self.monitor.wake()
        return {}

    def reset_network(self, body: dict) -> dict:
        net.reset_connections()
        return {"message": "已清空空闲连接和耗时统计；下一次请求重新连接"}

    def save_channel(self, body: dict) -> dict:
        ch = normalize_channel(body)
        if ch["type"] not in notify.CHANNEL_TYPES:
            raise ValueError("不认识的推送方式")
        notify.validate_channel(ch)

        def fn(cfg):
            for i, c in enumerate(cfg["channels"]):
                if c["id"] == ch["id"]:
                    if "message" not in body and "message" in c:
                        ch["message"] = copy.deepcopy(c["message"])
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
        net.configure(cfg)
        hits, where = self.monitor.test_hits(cfg)
        ctx = self.monitor.ctx(cfg, hits)
        notify.send(ch, hits, ctx)
        page = "，点开应该是推送页" if ctx.page_url else ""
        return {"message": f"已发送（{where}{page}），去看看收到没有"}

    def save_message(self, body: dict) -> dict:
        """单独保存布局，避免覆盖正在编辑的渠道凭据或关注。"""
        cid = str(body.get("channel_id") or "")
        value = body.get("message")
        if not body.get("inherit") and not isinstance(value, dict):
            raise ValueError("消息布局格式不对")
        layout = message_layout.normalize(value)
        if not body.get("inherit") and layout["enabled"] and not layout["blocks"]:
            raise ValueError("至少选一个消息模块，或关闭自定义布局")

        def fn(cfg):
            if not cid:
                cfg["message"] = None if body.get("inherit") else layout
                return
            ch = next((c for c in cfg["channels"] if c["id"] == cid), None)
            if ch is None:
                raise ValueError("这个渠道已被删除，请刷新后重选")
            if body.get("inherit"):
                ch.pop("message", None)
            else:
                ch["message"] = layout
        self.cfgstore.update(fn)
        return {"saved": True}

    def preview_message(self, body: dict) -> dict:
        """只读已有商品；不搜索、不发推送、不写入推送页。"""
        cfg = self.cfgstore.load()
        cid = str(body.get("channel_id") or "")
        ch = next((c for c in cfg["channels"] if c["id"] == cid), None)
        if cid and ch is None:
            raise ValueError("这个渠道已被删除，请刷新后重选")
        kind = ch["type"] if ch else body.get("type", "wxpusher")
        if kind not in notify.CHANNEL_TYPES:
            raise ValueError("不认识的推送方式")
        value = body.get("message", ch.get("message", cfg.get("message")) if ch else cfg.get("message"))
        if body.get("inherit") and ch:
            value = cfg.get("message")
        layout = message_layout.normalize(value)
        active = layout if layout and layout["enabled"] else None
        mode = body.get("sample", "latest")
        if mode not in ("latest", "example", "drop", "xianyu"):
            raise ValueError("不认识的示例商品")
        rows = self.store.feed(limit=1)
        row = rows[0] if rows else None
        example_image = row["image"] if row and row.get("image") else ""
        if mode == "latest" and row:
            it = Item(source=row["source"], id=row["item_id"], title=row["title"], price=row["price"],
                      url=row["url"], image=row["image"], extra=row["extra"])
            hit = notify.Hit(it, row["watch_id"], row["watch_name"], row["kind"], row["old_price"])
            sample_label = "最近发现的商品"
        else:
            xianyu = mode == "xianyu"
            it = Item(source="xianyu" if xianyu else "mercari", id="1234567890" if xianyu else "m12345678901",
                      title="五条悟 缶バッジ · 未拆封徽章套装", price=88 if xianyu else 1500,
                      url="https://www.goofish.com/item?id=1234567890" if xianyu else "https://jp.mercari.com/item/m12345678901",
                      image=example_image, extra="未拆封 · 包邮", created=int(time.time()) - 3600)
            hit = notify.Hit(it, "example", "五条悟周边", "drop" if mode == "drop" else "new",
                             2000 if mode == "drop" else None)
            sample_label = "闲鱼示例" if xianyu else "降价示例" if mode == "drop" else "日站示例"
        base = access.phone_base(cfg)
        ctx = notify.Ctx(rate=self.monitor.rate(cfg, fetch=False), buy=cfg["buy"], web_url=base,
                         page_url=f"{base}/p/preview" if base and cfg["web"]["push_page"] else "",
                         message=active, channel_kind=kind,
                         queries={w["id"]: query_of(w) for w in cfg["watches"]},
                         keywords={**{w["id"]: w["keyword"] for w in cfg["watches"]}, "example": "五条悟 缶バッジ"})
        if active:
            visual = notify._message_body(hit, ctx, 0, "html")
            warnings = message_layout.warnings(active, kind, bool(base and cfg["web"]["push_page"]))
        else:
            visual = notify._wxpusher_item(hit, ctx, 0) if kind == "wxpusher" else notify.html_list([hit], ctx)
            warnings = ["当前使用渠道原有格式；选择任意预设或模块即可开始自定义。"]
        if not it.image:
            warnings.append("示例没有配图；真实商品有图时会自动展示所选的配图模块。")
        if mode != "latest" or not row:
            warnings.append("这是示例商品，图片借用最近商品；不会发送通知。" if example_image else "这是示例商品，不会发送通知。")
        return {"html": visual, "text": notify.plain_lines([hit], ctx), "title": notify.summary_title([hit], ctx),
                "target": notify.link(hit, ctx, 0), "warnings": warnings, "sample_label": sample_label,
                "capabilities": message_layout.CAPABILITIES[kind], "custom": bool(active)}

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
        try:
            with self.monitor.paused(90):
                cookies_before = self.store.kv_get(KV_COOKIES)
                text, _ = migrate.run(path, data_dir, apply=True, store=self.store, cfgstore=self.cfgstore)
                if self.store.kv_get(KV_COOKIES) != cookies_before:
                    xianyu_account.attach(self.store)
        except TimeoutError:
            raise ValueError("正在扫描，过一会儿再试") from None
        self.monitor.trigger()
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
        cfg = self.cfgstore.load()
        net.configure(cfg)
        with self.monitor._gate:
            items = self.monitor.search(key, query_of(watch), cfg)
        rate = self.monitor.rate(cfg, fetch=False)
        return {"items": [dict(it.to_dict(), passes=passes(watch, it, rate)) for it in items[:60]],
                "total": len(items)}

    def scan(self, body: dict) -> dict:
        self.monitor.trigger()
        return {}

    def block(self, body: dict) -> dict:
        """在上新里屏蔽商品，顺便给关注加必含词、排除词。

        body：items [{source, item_id, watch_id}]；watches {关注 ID: {must_add: [...], exclude_add: [...]}}；
        hide_failing 为真时，上新里这些关注下其他不符合新条件的记录也一起屏蔽。
        返回撤销要用的东西：屏蔽了哪些商品、改之前的关注。
        """
        items = [(str(i.get("source") or ""), str(i.get("item_id") or ""), str(i.get("watch_id") or ""))
                 for i in body.get("items") or [] if isinstance(i, dict)]
        items = [i for i in items if i[0] and i[1]]
        changes = body.get("watches") or {}
        if not isinstance(changes, dict):
            raise ValueError("请求格式不对")
        before, after = [], []

        def fn(cfg):
            for w in cfg["watches"]:
                ch = changes.get(w["id"])
                if not isinstance(ch, dict):
                    continue
                must = _merge_words(w["must"], ch.get("must_add"))
                exclude = _merge_words(w["exclude"], ch.get("exclude_add"))
                if must != w["must"] or exclude != w["exclude"]:
                    before.append(copy.deepcopy(w))
                    w["must"], w["exclude"] = must, exclude
                    after.append(copy.deepcopy(w))
        if changes:
            self.cfgstore.update(fn)
        if body.get("hide_failing"):
            ids = {w["id"]: w for w in after}
            have = {(s, i) for s, i, _ in items}
            for wid, w in ids.items():
                for r in self.store.feed(watch_id=wid, limit=500):
                    it = Item(source=r["source"], id=r["item_id"], title=r["title"], price=None, url="")
                    if (r["source"], r["item_id"]) not in have and not passes(w, it):
                        have.add((r["source"], r["item_id"]))
                        items.append((r["source"], r["item_id"], wid))
        n = self.store.block(items)
        return {"blocked": n, "watches": [w["name"] for w in after], "undo": {
            "items": [{"source": s, "item_id": i} for s, i, _ in items], "watches": before}}

    def unblock(self, body: dict) -> dict:
        """撤销刚才的屏蔽：放回商品，关注改回原样。"""
        self.store.unblock([(str(i.get("source")), str(i.get("item_id")))
                            for i in body.get("items") or [] if isinstance(i, dict)])
        olds = {w.get("id"): w for w in body.get("watches") or [] if isinstance(w, dict)}
        if olds:
            def fn(cfg):
                for k, w in enumerate(cfg["watches"]):
                    if w["id"] in olds:
                        cfg["watches"][k] = normalize_watch(olds[w["id"]])
            self.cfgstore.update(fn)
        return {}

    def clear_blocked(self, body: dict) -> dict:
        self.store.clear_blocked()
        return {}

    def clear_feed(self, body: dict) -> dict:
        self.store.clear_feed()
        return {}

    # —— 闲鱼账号 ——
    def xianyu_qr(self, body: dict) -> dict:
        return {"code": xianyu_account.qr_start()}

    def xianyu_poll(self, body: dict) -> dict:
        r = xianyu_account.qr_poll()
        if r["status"] == "done":
            r.update(self._xianyu_ready())
        return r

    def xianyu_cookie(self, body: dict) -> dict:
        xianyu_account.use_cookie_text(str(body.get("cookie") or ""))
        return {"status": "done", **self._xianyu_ready()}

    def _xianyu_ready(self) -> dict:
        """刚登录好：拿第一个关注的关键词试搜一次，搜得了就马上扫一轮闲鱼。"""
        cfg = self.cfgstore.load()
        kw = next((w["keyword"] for w in cfg["watches"] if "xianyu" in w["sources"] and w["keyword"]),
                  next((w["keyword"] for w in cfg["watches"] if w["keyword"]), "CD"))
        src = SOURCES["xianyu"]
        try:
            with src.lock:
                items = src.search(Query(kw))
            check, ok = f"试搜了一下「{kw}」，搜到 {len(items)} 件，能用了", True
        except Exception as e:
            check, ok = f"登录好了，但试搜「{kw}」没成功：{e}", False
        finally:
            self.monitor._last_request["xianyu"] = time.time()
        self.monitor.rescan_source("xianyu")
        return {"check": check, "check_ok": ok, **xianyu_account.state()}

    def xianyu_cancel(self, body: dict) -> dict:
        xianyu_account.qr_cancel()
        return {}

    def xianyu_logout(self, body: dict) -> dict:
        xianyu_account.logout()
        return {}

    def xianyu_add(self, body: dict) -> dict:
        """登录好以后，一键把闲鱼加进所有关注。"""
        added = []

        def fn(cfg):
            for w in cfg["watches"]:
                if "xianyu" not in w["sources"]:
                    w["sources"].append("xianyu")
                    added.append(w["name"])
        self.cfgstore.update(fn)
        self.monitor.rescan_source("xianyu")
        return {"added": len(added)}


POSTS = {
    "/api/watch": "save_watch", "/api/watch/delete": "delete_watch",
    "/api/settings": "save_settings",
    "/api/network/reset": "reset_network",
    "/api/channel": "save_channel", "/api/channel/delete": "delete_channel",
    "/api/channel/test": "test_channel",
    "/api/message/settings": "save_message", "/api/message/preview": "preview_message",
    "/api/preview": "preview", "/api/scan": "scan", "/api/feed/clear": "clear_feed",
    "/api/access": "save_access", "/api/migrate": "migrate",
    "/api/block": "block", "/api/unblock": "unblock", "/api/blocked/clear": "clear_blocked",
    "/api/xianyu/qr": "xianyu_qr", "/api/xianyu/poll": "xianyu_poll", "/api/xianyu/cookie": "xianyu_cookie",
    "/api/xianyu/cancel": "xianyu_cancel", "/api/xianyu/logout": "xianyu_logout", "/api/xianyu/add": "xianyu_add",
}


def _merge_words(old: list, add) -> list:
    """把新词并进必含词 / 排除词：去掉空的、太长的、已经有的（全半角大小写不同也算有）。"""
    out = list(old)
    have = {norm(w) for w in out}
    for w in add if isinstance(add, list) else []:
        w = str(w).strip()
        if w and len(w) <= 60 and norm(w) not in have:
            have.add(norm(w))
            out.append(w)
    return out


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
            assets = {"/static/message-designer.js": "text/javascript; charset=utf-8",
                      "/static/message-designer.css": "text/css; charset=utf-8"}
            if url.path in assets:
                return self._send(200, (STATIC / url.path.rsplit("/", 1)[1]).read_bytes(), assets[url.path])
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

