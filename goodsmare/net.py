"""HTTP：只用标准库 urllib。代理、gzip、编码、被拦截的识别都在这里。"""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_lock = threading.Lock()
_proxy = ""                  # 设置里填的代理（补全了 http://）；空着就跟系统代理走
SYS_TTL = 10                 # 系统代理隔几秒重读一次
_sys_cache: tuple | None = None
# 直连连不上时，到本机这些端口找代理软件：Clash 7890、Clash Verge 7897、v2rayN 10809……
LOCAL_PROXY_PORTS = (7890, 7897, 7891, 10809, 1087, 8889, 20171, 6152)

# 调试用：设了目录就把每次响应原样存下来，页面改版时拿去对照。
dump_dir: Path | None = None


class FetchError(Exception):
    kind = "http"     # 给界面归类：net 连不上、blocked 被拦、http 其他 HTTP 错误、login 要登录


class Unreachable(FetchError):
    """连不上：超时、拒绝连接、域名解析失败。国内直连煤炉、雅虎拍卖这些就是这样。"""
    kind = "net"

    def __init__(self, msg: str, host: str = ""):
        super().__init__(msg)
        self.host = host


class Blocked(FetchError):
    """对方返回了验证页 / 限流页，不是我们解析的问题。"""
    kind = "blocked"


class Response:
    def __init__(self, status: int, url: str, headers, body: bytes):
        self.status = status
        self.url = url
        self.headers = headers
        self.body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        charset = None
        ctype = self.headers.get("Content-Type", "") if self.headers else ""
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            charset = m.group(1)
        else:
            m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", self.body[:4096], re.I)
            if m:
                charset = m.group(1).decode("ascii", "ignore")
        try:
            return self.body.decode(charset or "utf-8", errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text())

    def check(self, site: str) -> "Response":
        """非 2xx 就抛错；看得出是被拦的就抛 Blocked。"""
        if self.ok:
            return self
        head = self.body[:3000].decode("utf-8", "ignore")
        if self.status in (403, 429, 503) and _looks_blocked(head):
            raise Blocked(f"{site} 拦下了请求（HTTP {self.status}，像是人机验证/限流），"
                          "过一阵再试，或者在设置里换个代理")
        if self.status == 429:
            raise Blocked(f"{site} 说请求太频繁（HTTP 429），把扫描间隔调大一点")
        raise FetchError(f"{site} 返回 HTTP {self.status}")


def _looks_blocked(head: str) -> bool:
    markers = ("Just a moment", "cf-chl", "challenge-platform", "Attention Required",
               "captcha", "Access Denied", "アクセスが集中", "Too Many Requests")
    return any(m.lower() in head.lower() for m in markers)


def set_proxy(proxy: str) -> None:
    """设置里填的代理。空着就跟系统走：环境变量 HTTPS_PROXY 等，Windows、macOS 的系统代理设置。"""
    global _proxy
    proxy = (proxy or "").strip()
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy
    with _lock:
        _proxy = proxy


def system_proxies() -> dict:
    """系统代理。Clash 之类的「系统代理」开关随时会拨，隔几秒重读一次：

    只在启动时读的话，启动那会儿开关正好关着，之后就一直直连，煤炉、雅虎拍卖全都超时。
    """
    global _sys_cache
    now = time.monotonic()
    with _lock:
        if _sys_cache is not None and now - _sys_cache[0] < SYS_TTL:
            return _sys_cache[1]
    try:
        found = urllib.request.getproxies()
    except Exception:   # 读注册表、环境变量出了问题就当没有
        found = {}
    found = {k: v for k, v in found.items() if k in ("http", "https") and v}
    with _lock:
        _sys_cache = (now, found)
    return found


def _proxies() -> dict:
    with _lock:
        explicit = _proxy
    return {"http": explicit, "https": explicit} if explicit else system_proxies()


def proxy_info() -> dict:
    """现在实际怎么连：app 设置里填的代理，system 系统代理，direct 直连。"""
    with _lock:
        explicit = _proxy
    if explicit:
        return {"mode": "app", "proxy": explicit}
    found = system_proxies()
    url = found.get("https") or found.get("http") or ""
    return {"mode": "system", "proxy": url} if url else {"mode": "direct", "proxy": ""}


def find_local_proxy(host: str) -> str:
    """在本机常见端口上找一个能连到 host 的 HTTP 代理（发一个 CONNECT 试试），找不到返回空串。"""
    for port in LOCAL_PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3) as s:
                s.settimeout(5)
                s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
                head = s.recv(256).decode("latin-1", "ignore")
        except OSError:
            continue
        if re.match(r"HTTP/1\.[01] 200", head):
            return f"http://127.0.0.1:{port}"
    return ""


def _opener_for(cookies: http.cookiejar.CookieJar | None, direct: bool):
    handlers: list = [urllib.request.ProxyHandler({} if direct else _proxies())]
    if cookies is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookies))
    return urllib.request.build_opener(*handlers)


def _reason(e: BaseException) -> str:
    r = getattr(e, "reason", e)
    if isinstance(r, (socket.timeout, TimeoutError)) or "timed out" in str(r):
        return "超时"
    if isinstance(r, ConnectionRefusedError):
        return "连接被拒绝"
    if isinstance(r, ConnectionResetError):
        return "连接被重置"
    if isinstance(r, socket.gaierror):
        return "域名解析失败"
    if isinstance(r, ssl.SSLError):
        return "加密连接失败"
    m = re.search(r"Tunnel connection failed: (\d+)", str(r))
    if m:
        return f"代理转发失败，HTTP {m.group(1)}"
    return str(r) or type(r).__name__


def _unreachable(e: BaseException, url: str, direct: bool) -> Unreachable:
    parts = urllib.parse.urlsplit(url)
    via = "" if direct else _proxies().get(parts.scheme, "")
    reason = _reason(e)
    if via and isinstance(getattr(e, "reason", e), ConnectionRefusedError):
        msg = f"代理 {via} 连不上（连接被拒绝），代理软件开着吗"
    else:
        msg = f"连不上 {parts.netloc}（{reason}）"
    return Unreachable(msg, parts.hostname or "")


def request(method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
            json_body=None, data: bytes | dict | None = None, timeout: float = 20,
            retries: int = 1, cookies: http.cookiejar.CookieJar | None = None,
            direct: bool = False) -> Response:
    """cookies：要记住 cookie 的网站自己带一个 CookieJar 来，别的网站照旧不存。

    direct：不走代理（闲鱼这种国内网站，登录态别从国外节点出去）。
    """
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9,zh-CN;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        hdrs.update(headers)
    body = None
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif data is not None:
        body = data

    last_err: Exception | None = None
    opener = _opener_for(cookies, direct)
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                result = Response(resp.status, resp.geturl(), resp.headers, _decompress(raw, resp.headers))
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp else b""
            result = Response(e.code, url, e.headers, _decompress(raw, e.headers))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise _unreachable(e, url, direct) from e
        _dump(url, result)
        if result.status >= 500 and attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            continue
        return result
    raise FetchError(str(last_err))


def get(url: str, **kw) -> Response:
    return request("GET", url, **kw)


def post(url: str, **kw) -> Response:
    return request("POST", url, **kw)


def _decompress(raw: bytes, headers) -> bytes:
    enc = (headers.get("Content-Encoding", "") if headers else "").lower()
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        pass
    return raw


def _dump(url: str, resp: Response) -> None:
    if dump_dir is None:
        return
    dump_dir.mkdir(parents=True, exist_ok=True)
    host = urllib.parse.urlsplit(url).netloc.replace(":", "_")
    ext = "json" if resp.body[:1] in (b"{", b"[") else "html"
    path = dump_dir / f"{time.strftime('%H%M%S')}-{host}-{resp.status}.{ext}"
    path.write_bytes(resp.body)
