"""闲鱼（goofish.com）：走网页版用的 mtop 接口。

不登录搜不了：接口直接回一个登录页地址。所以要先在「设置 → 闲鱼账号」用闲鱼 App 扫码，
扫不了的话也可以从浏览器里复制 cookie 贴进来。登录态（cookie）存在本程序的数据库里，只在这台电脑上，
程序重启、迁移都带着走。

mtop 的签名是 md5(token&毫秒时间戳&appKey&data)，token 是 cookie _m_h5_tk 下划线前面那段。
token 没有或者过期时，接口回 FAIL_SYS_TOKEN_EMPTY / FAIL_SYS_TOKEN_EXOIRED，同时下发新的 _m_h5_tk，重签一次就行。
扫码登录照着网页版的登录框走：mini_login.htm 拿表单参数 → generate.do 拿二维码 → query.do 轮询，
确认后 passport 下发的 cookie 就是登录态（2026 年 10 月对过：生成二维码、轮询这两步不登录也能跑通）。

闲鱼的价格是人民币。国内网站，请求一律直连、不走代理，免得登录态从国外节点出去被风控。
"""

from __future__ import annotations

import hashlib
import html
import http.cookiejar
import json
import random
import re
import threading
import time
import urllib.parse

from .. import net
from .base import Item, NoResults, Query, Source, walk_json

APP_KEY = "34839810"
H5API = "https://h5api.m.goofish.com/h5/{api}/1.0/"
SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
ORIGIN = "https://www.goofish.com"
PASSPORT = "https://passport.goofish.com"
HEADERS = {"Origin": ORIGIN, "Referer": ORIGIN + "/", "Accept-Language": "zh-CN,zh;q=0.9"}
KV_COOKIES, KV_NICK, KV_AT = "xianyu_cookies", "xianyu_nick", "xianyu_login_at"
LOGIN_HINT = "到「设置 → 闲鱼账号」用闲鱼 App 扫一下码"


class NeedLogin(net.FetchError):
    kind = "login"


def sign(token: str, t: str, data: str) -> str:
    return hashlib.md5(f"{token}&{t}&{APP_KEY}&{data}".encode("utf-8")).hexdigest()


def make_cookie(name: str, value: str, domain: str = ".goofish.com", path: str = "/",
                expires: int | None = None, secure: bool = False) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(0, name, value, None, False, domain, True, domain.startswith("."), path, True,
                                 secure, expires, False, None, None, {})


def parse_cookie_header(text: str) -> list[tuple[str, str]]:
    """浏览器里复制来的 cookie：「a=1; b=2」，带着开头的「cookie:」也行。"""
    text = re.sub(r"^\s*cookie\s*:\s*", "", text or "", flags=re.I)
    out = []
    for part in text.replace("\n", ";").split(";"):
        name, eq, value = part.strip().partition("=")
        if eq and name.strip():
            out.append((name.strip(), value.strip()))
    return out


def _nick(jar) -> str:
    """登录后 cookie 里有昵称，写成 %5Cu6d4b 这样的转义。"""
    for name in ("tracknick", "_nk_", "lgc"):
        for c in jar:
            if c.name == name and c.value:
                v = urllib.parse.unquote(c.value)
                return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), v)
    return ""


def _error(ret: str, d: dict) -> net.FetchError:
    url = str((d.get("data") or {}).get("url") or "") if isinstance(d.get("data"), dict) else ""
    challenged = "RGV587" in ret or "USER_VALIDATE" in ret
    if "SESSION_EXPIRED" in ret or (challenged and "login" in url.lower()):
        return NeedLogin(f"闲鱼的登录失效了，要重新登录：{LOGIN_HINT}")
    if challenged:
        return net.Blocked("闲鱼要求滑块验证（风控），这一轮先跳过；老是这样的话把扫描间隔调大一点")
    return net.FetchError(f"闲鱼返回：{ret or '空'}")


class Account:
    """闲鱼的登录态：一个 CookieJar，存在数据库的 kv 里。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.jar = http.cookiejar.CookieJar()
        self.store = None
        self.nick = ""
        self.login_at = 0
        self._saved = ""
        self._qr: dict | None = None    # 扫码登录进行中

    # —— 存取 ——
    def attach(self, store) -> None:
        with self.lock:
            self._qr = None
            self.store = store
            self.jar = http.cookiejar.CookieJar()
            raw = store.kv_get(KV_COOKIES)
            try:
                rows = json.loads(raw) if raw else []
            except ValueError:
                rows = []
            for r in rows:
                if isinstance(r, dict) and r.get("name"):
                    self.jar.set_cookie(make_cookie(str(r["name"]), str(r.get("value") or ""),
                                                    r.get("domain") or ".goofish.com", r.get("path") or "/",
                                                    r.get("expires"), bool(r.get("secure"))))
            self.nick = store.kv_get(KV_NICK)
            try:
                self.login_at = int(float(store.kv_get(KV_AT, "0") or 0))
            except ValueError:
                self.login_at = 0
            self._saved = raw

    def save(self) -> None:
        """cookie 变了才写库：接口隔一阵会换一个新的 _m_h5_tk。"""
        with self.lock:
            if self.store is None:
                return
            raw = json.dumps([{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                               "expires": c.expires, "secure": c.secure} for c in self.jar], ensure_ascii=False)
            if raw != self._saved:
                self.store.kv_set(KV_COOKIES, raw)
                self._saved = raw

    def logged_in(self) -> bool:
        with self.lock:
            return any(c.name == "unb" and c.value for c in self.jar)

    def state(self) -> dict:
        with self.lock:
            return {"logged_in": self.logged_in(), "nick": self.nick if self.logged_in() else "",
                    "at": self.login_at if self.logged_in() else 0, "scanning": self._qr is not None}

    def adopt(self, cookies) -> None:
        """登录成功：整个换成新的 cookie。"""
        with self.lock:
            self._qr = None
            self.jar = http.cookiejar.CookieJar()
            for c in cookies:
                self.jar.set_cookie(c)
            self.nick = _nick(self.jar)
            self.login_at = int(time.time())
            if self.store is not None:
                self.store.kv_set(KV_NICK, self.nick)
                self.store.kv_set(KV_AT, str(self.login_at))
            self.save()

    def use_cookie_text(self, text: str) -> None:
        pairs = parse_cookie_header(text)
        if not any(n == "unb" and v for n, v in pairs):
            raise ValueError("这段 cookie 里没有登录信息（找不到 unb）。要先在浏览器里登录闲鱼，再整段复制过来")
        self.adopt([make_cookie(n, v) for n, v in pairs])

    def logout(self) -> None:
        with self.lock:
            self.jar = http.cookiejar.CookieJar()
            self.nick, self.login_at, self._qr = "", 0, None
            if self.store is not None:
                for k in (KV_COOKIES, KV_NICK, KV_AT):
                    self.store.kv_set(k, "")
            self._saved = ""

    # —— mtop 接口 ——
    @staticmethod
    def token(jar) -> str:
        for c in jar:
            if c.name == "_m_h5_tk" and c.value:
                return c.value.split("_")[0]
        return ""

    def renew(self, jar, before: dict) -> bool:
        """登录过期时续一次：网页版用 passport 的 hasLogin，拿长期登录凭证换一套新的 cookie。

        before 是出错前的 cookie（出错的响应常常顺手把 unb 删了）。续上了返回 True；没法实测，续不上就照旧要重新扫码。
        """
        if not before.get("unb"):
            return False
        form = {"hid": before["unb"], "ltl": "true", "appName": "xianyu", "appEntrance": "web",
                "_csrf_token": before.get("XSRF-TOKEN", ""), "umidToken": "", "hsiz": before.get("cookie2", ""),
                "bizParams": "taobaoBizLoginFrom=web", "mainPage": "false", "isMobile": "false", "lang": "zh_CN",
                "returnUrl": "", "fromSite": "77", "isIframe": "true", "documentReferer": ORIGIN + "/",
                "defaultView": "hasLogin", "umidTag": "SERVER", "deviceId": before.get("cna", "")}
        try:
            d = net.post(PASSPORT + "/newlogin/hasLogin.do?appName=xianyu&fromSite=77", data=form, headers=HEADERS,
                         cookies=jar, direct=True, retries=0).check("闲鱼登录").json()
        except (net.FetchError, ValueError):
            return False
        if not (isinstance(d, dict) and (d.get("content") or {}).get("success")):
            return False
        if not any(c.name == "unb" and c.value for c in jar):
            jar.set_cookie(make_cookie("unb", before["unb"]))   # unb 只是账号编号，被删了就补回去
        with self.lock:
            if self.jar is jar:
                self.save()
        return True

    def call(self, api: str, data: dict) -> dict:
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        with self.lock:
            jar = self.jar
        before = {c.name: c.value for c in jar}
        renewed = False
        for _ in range(4):
            t = str(int(time.time() * 1000))
            params = {"jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign(self.token(jar), t, body), "v": "1.0",
                      "type": "originaljson", "accountSite": "xianyu", "dataType": "json", "timeout": "20000",
                      "api": api, "sessionOption": "AutoLoginOnly"}
            d = net.post(H5API.format(api=api), params=params, data={"data": body},
                         headers={**HEADERS, "Accept": "application/json"}, cookies=jar, direct=True,
                         retries=0).check("闲鱼").json()
            ret = " ".join(str(r) for r in d.get("ret") or [])
            if ret.startswith("SUCCESS"):
                self.save()
                return d
            if "TOKEN_EMPTY" in ret or "TOKEN_EXOIRED" in ret or "TOKEN_EXPIRED" in ret:
                continue    # 这次的响应已经带了新的 _m_h5_tk，重签一次
            error = _error(ret, d)
            if isinstance(error, NeedLogin) and not renewed and self.renew(jar, before):
                renewed = True
                continue    # 续上了，再查一次
            raise error
        raise net.FetchError("闲鱼的接口令牌拿不到，过一会儿再试")

    # —— 扫码登录 ——
    def qr_start(self) -> str:
        """开始扫码登录，返回二维码里的内容（一个网址）。"""
        jar = http.cookiejar.CookieJar()
        page = net.get(PASSPORT + "/mini_login.htm", params={
            "lang": "zh_cn", "appName": "xianyu", "appEntrance": "web", "styleType": "vertical",
            "notLoadSsoView": "false", "notKeepLogin": "false", "isMobile": "false", "qrCodeFirst": "false",
            "stie": "77", "rnd": str(random.random())}, headers=HEADERS, cookies=jar, direct=True).check("闲鱼登录").text()
        m = re.search(r"window\.viewData\s*=\s*(\{.*?\});", page, re.S)
        form = {}
        if m:
            try:
                form = json.loads(m.group(1)).get("loginFormData") or {}
            except ValueError:
                form = {}
        if not form:
            raise net.FetchError("闲鱼的登录页改版了，扫码登录用不了；先用「复制 cookie」的办法")
        form["umidTag"] = "SERVER"
        # 表单里有空值（bizParams 之类），原样带上：net.request 的 params 会把空值去掉
        d = net.get(PASSPORT + "/newlogin/qrcode/generate.do?" + urllib.parse.urlencode(form),
                    headers=HEADERS, cookies=jar, direct=True).check("闲鱼登录").json()
        data = (d.get("content") or {}).get("data") or {}
        if not data.get("codeContent") or not data.get("ck"):
            raise net.FetchError(f"闲鱼没给二维码：{json.dumps(d, ensure_ascii=False)[:160]}")
        form.update(t=data["t"], ck=data["ck"])
        with self.lock:
            self._qr = {"jar": jar, "form": form, "code": data["codeContent"], "at": time.time()}
        return data["codeContent"]

    def qr_poll(self) -> dict:
        """问一次扫码进行到哪了：new 等扫、scanned 等手机上确认、done 登录好了，
        expired / canceled / verify（闲鱼要再验证一下身份）/ error。"""
        with self.lock:
            qr = self._qr
        if qr is None:
            return {"status": "none"}
        d = net.post(PASSPORT + "/newlogin/qrcode/query.do", data=qr["form"], headers=HEADERS,
                     cookies=qr["jar"], direct=True, retries=0).check("闲鱼登录").json()
        data = (d.get("content") or {}).get("data") or {}
        st = str(data.get("qrCodeStatus") or "")
        with self.lock:
            # 请求在途时可能取消、退出或开始了新一轮扫码，旧结果不能覆盖当前账号。
            # 检查和保存登录信息放在同一把锁里，中间不能被退出登录打断。
            if self._qr is not qr:
                return {"status": "none"}
            if st == "NEW":
                return {"status": "new"}
            if st == "SCANED":
                return {"status": "scanned"}
            self._qr = None
            if st == "CONFIRMED":
                if data.get("iframeRedirect"):
                    return {"status": "verify", "url": str(data.get("iframeRedirectUrl") or "")}
                cookies = list(qr["jar"])
                if not any(c.name == "unb" and c.value for c in cookies):
                    return {"status": "error", "message": "手机上确认了，但闲鱼没给登录信息，再扫一次试试"}
                self.adopt(cookies)
                return {"status": "done"}
            if st == "EXPIRED":
                return {"status": "expired"}
            if st == "CANCELED":
                return {"status": "canceled"}
            return {"status": "error", "message": f"闲鱼返回：{st or json.dumps(d, ensure_ascii=False)[:160]}"}

    def qr_cancel(self) -> None:
        with self.lock:
            self._qr = None


account = Account()


def _num(v) -> float | None:
    if v is None:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万)?", str(v).replace(",", ""))
    if not m:
        return None
    return float(m.group(1)) * (10000 if m.group(2) else 1)


def _price(ex: dict, args: dict) -> int | None:
    """价格是人民币，可能带角分；存成整数元（四舍五入）。"""
    p = _num(args.get("price"))
    if p is None:
        parts = ex.get("price")
        text = ("".join(str(x.get("text", "")) for x in parts if isinstance(x, dict))
                if isinstance(parts, list) else str(parts or ""))
        p = _num(text.replace("当前价", ""))
    return None if p is None else int(p + 0.5)


def _image(url) -> str:
    url = str(url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url if url.startswith("https://") else ""


class Xianyu(Source):
    key = "xianyu"
    name = "闲鱼"
    home = ORIGIN + "/"
    currency = "CNY"
    login = True
    min_gap = 5.0          # 用的是自己的账号，请求放慢点，别被风控
    min_interval = 300     # 同一个关注 5 分钟最多搜一次

    def ready(self) -> bool:
        return account.logged_in()

    def search_url(self, q: Query) -> str:
        return f"{ORIGIN}/search?q={urllib.parse.quote(q.keyword)}"

    def search(self, q: Query) -> list[Item]:
        if not account.logged_in():
            raise NeedLogin(f"闲鱼要登录才能搜：{LOGIN_HINT}")
        d = account.call(SEARCH_API, {
            "pageNumber": 1, "keyword": q.keyword, "fromFilter": False, "rowsPerPage": 30,
            "sortValue": "desc", "sortField": "create", "customDistance": "", "gps": "",
            "propValueStr": {}, "customGps": "", "searchReqFromPage": "pcSearch",
            "extraFilterValue": "{}", "userPositionJson": "{}"})
        return self.parse(d)

    @staticmethod
    def parse(d: dict) -> list[Item]:
        rows = (d.get("data") or {}).get("resultList") or []
        if not rows:
            return NoResults()
        items = []
        for r in rows:
            main = (((r.get("data") or {}).get("item") or {}).get("main") or {}) if isinstance(r, dict) else {}
            it = _make(main.get("exContent") or {}, (main.get("clickParam") or {}).get("args") or {})
            if it is not None:
                items.append(it)
        if not items:
            # 结构变了：在整个结果里找带商品 ID 和标题的那一层
            for ex in walk_json(rows, lambda x: str(x.get("itemId") or "").isdigit() and bool(x.get("title"))):
                it = _make(ex, {})
                if it is not None and all(it.id != o.id for o in items):
                    items.append(it)
        return items


def _make(ex: dict, args: dict) -> Item | None:
    item_id = str(ex.get("itemId") or args.get("item_id") or args.get("id") or "").strip()
    if not item_id.isdigit():      # 广告、推荐词之类的卡片
        return None
    title = html.unescape(re.sub(r"<[^>]+>", "", str(ex.get("title") or ""))).strip()
    extra = []
    if ex.get("area"):
        extra.append(str(ex["area"]))
    if args.get("tag") == "freeship" or "包邮" in json.dumps(ex.get("fishTags") or {}, ensure_ascii=False):
        extra.append("包邮")
    want = _num(args.get("wantNum") or ex.get("wantNum"))
    if want:
        extra.append(f"{int(want)}人想要")
    published = _num(args.get("publishTime") or ex.get("publishTime")) or 0
    return Item(
        source="xianyu", id=item_id, title=title, price=_price(ex, args),
        url=f"{ORIGIN}/item?id={item_id}", image=_image(ex.get("picUrl") or ex.get("mainPic")),
        extra=" ".join(extra), created=int(published / 1000 if published > 1e11 else published),
    )
