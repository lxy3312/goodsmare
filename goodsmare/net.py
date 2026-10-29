"""HTTP：只用标准库 urllib。代理、gzip、编码、被拦截的识别都在这里。"""

from __future__ import annotations

import gzip
import collections
import contextlib
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
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import transport

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_lock = threading.Lock()
_proxy = ""                  # 设置里填的代理（补全了 http://）；空着就跟系统代理走
SYS_TTL = 10                 # 系统代理隔几秒重读一次
_sys_cache: tuple | None = None
_network = {"keepalive": True, "connect_timeout": 8.0, "read_timeout": 20.0,
            "routes": {"domestic_push": "direct", "xianyu": "direct"}}
_local = threading.local()
_metrics: dict[str, collections.deque] = {}
DOMESTIC_PUSH = ("wxpusher.zjiecode.com", "xtuis.cn", "iyuu.cn", "push.i-i.me", "pushplus.plus",
                 "ftqq.com", "qyapi.weixin.qq.com", "oapi.dingtalk.com", "open.feishu.cn")
OVERSEAS_PUSH = ("api.telegram.org", "discord.com", "discordapp.com", "api.pushover.net", "api.day.app", "ntfy.sh")
# 直连连不上时，到本机这些端口找代理软件：Clash 7890、Clash Verge 7897、v2rayN 10809……
LOCAL_PROXY_PORTS = (7890, 7897, 7891, 10809, 1087, 8889, 20171, 6152)

# 调试用：设了目录就把每次响应原样存下来，页面改版时拿去对照。
dump_dir: Path | None = None


class FetchError(Exception):
    kind = "http"     # 给界面归类：net 连不上、blocked 被拦、http 其他 HTTP 错误、login 要登录

    def __init__(self, message: str, *, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


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

    def retry_after(self) -> float:
        value = next((v for k, v in (self.headers or {}).items() if k.lower() == "retry-after"), "")
        try:
            if str(value).strip().isdigit():
                return float(int(value))
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError, AttributeError):
            return 0

    def check(self, site: str) -> "Response":
        """非 2xx 就抛错；看得出是被拦的就抛 Blocked。"""
        if self.ok:
            return self
        head = self.body[:3000].decode("utf-8", "ignore")
        if self.status in (403, 429, 503) and _looks_blocked(head):
            raise Blocked(f"{site} 拦下了请求（HTTP {self.status}，像是人机验证/限流），"
                          "已暂缓请求，稍后再试", retry_after=self.retry_after())
        if self.status == 429:
            raise Blocked(f"{site} 说请求太频繁（HTTP 429），已暂缓请求", retry_after=self.retry_after())
        raise FetchError(f"{site} 返回 HTTP {self.status}", retry_after=self.retry_after())


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
        changed = _proxy != proxy
        _proxy = proxy
    if changed:
        reset_connections()


def configure(cfg: dict) -> None:
    from .config import normalize_network
    global _network
    set_proxy(cfg.get("proxy", ""))
    options = normalize_network(cfg.get("network"))
    with _lock:
        changed = options != _network
        _network = options
    if changed:
        reset_connections()


@contextlib.contextmanager
def route(name: str):
    previous = getattr(_local, "route", None)
    _local.route = name
    try:
        yield
    finally:
        _local.route = previous


def request_group(url: str) -> str:
    explicit = getattr(_local, "route", None)
    if explicit:
        return explicit
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for name, domains in (("domestic_push", DOMESTIC_PUSH), ("overseas_push", OVERSEAS_PUSH)):
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return name
    return "other"


def _route_proxies(group: str, direct: bool) -> dict:
    with _lock:
        choice = _network["routes"].get(group, "inherit")
    if direct or group == "xianyu" or choice == "direct":
        return {}
    if choice == "inherit":
        return _proxies()
    return {"http": choice, "https": choice}


def _redact_proxy(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
    except ValueError:
        return "代理地址无效"


def _record(group: str, elapsed: float, status: int, timing: dict, error: str = ""):
    sample = {"seconds": elapsed, "status": status, "error": error, **timing}
    with _lock:
        _metrics.setdefault(group, collections.deque(maxlen=100)).append(sample)


def network_info() -> dict:
    with _lock:
        samples = {key: list(values) for key, values in _metrics.items()}
        options = {**_network, "routes": dict(_network["routes"])}
    rows = {}
    for group, values in samples.items():
        def median(field, values=values):
            numbers = sorted(v[field] for v in values if field in v)
            return round(numbers[len(numbers) // 2], 3) if numbers else None
        measured = [v for v in values if "reused" in v]
        proxy = values[-1].get("proxy", "")
        rows[group] = {"samples": len(values), "median": median("seconds"), "connect": median("connect"),
                       "response": median("response"), "download": median("download"),
                       "reuse_percent": round(100 * sum(v["reused"] for v in measured) / len(measured)) if measured else None,
                       "errors": sum(bool(v["error"]) or v["status"] >= 400 for v in values),
                       "last_status": values[-1]["status"], "proxy": _redact_proxy(proxy) if proxy else ""}
    return {"keepalive": options["keepalive"], "groups": rows}


def reset_connections():
    transport.POOL.clear()
    with _lock:
        _metrics.clear()


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
    group = getattr(_local, "request_group", getattr(_local, "route", None) or "other")
    with _lock:
        options = dict(_network)
    handlers: list = [urllib.request.ProxyHandler(_route_proxies(group, direct)),
                     transport.HTTPHandler(options, cookies), transport.HTTPSHandler(options, cookies)]
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
    via = _route_proxies(request_group(url), direct).get(parts.scheme, "")
    reason = _reason(e)
    if via and isinstance(getattr(e, "reason", e), ConnectionRefusedError):
        msg = f"代理 {_redact_proxy(via)} 连不上（连接被拒绝），代理软件开着吗"
    else:
        msg = f"连不上 {parts.netloc}（{reason}）"
    return Unreachable(msg, parts.hostname or "")


def request(method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
            json_body=None, data: bytes | dict | None = None, timeout: float | None = None,
            retries: int | None = None, cookies: http.cookiejar.CookieJar | None = None,
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

    # 写请求默认不重发，避免服务端已受理但响应断线时造成重复通知。
    if retries is None:
        retries = 1 if method.upper() in ("GET", "HEAD", "OPTIONS") else 0
    if timeout is None:
        with _lock:
            timeout = max(_network["connect_timeout"], _network["read_timeout"])
    group = request_group(url)
    _local.request_group = group
    try:
        opener = _opener_for(cookies, direct)
    finally:
        del _local.request_group
    started = time.perf_counter()
    attempt = 0          # 第几次正常重试
    sent = 0             # 实际发出去几次
    fresh = False        # 这一次不用池子里的旧连接
    stale_retried = False
    while True:
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        timing = {}
        sent += 1
        try:
            with transport.fresh_connection(fresh), opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                result = Response(resp.status, resp.geturl(), resp.headers, _decompress(raw, resp.headers))
                timing = getattr(resp, "network_timing", {})
        except urllib.error.HTTPError as e:
            with e:
                timing = getattr(e.fp, "network_timing", {})
                raw = e.read() if e.fp else b""
                result = Response(e.code, url, e.headers, _decompress(raw, e.headers))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            fresh = False
            # 复用的旧连接还没回应就断了：对方刚好关掉了闲置连接，请求没被处理。换新连接马上再发一次，
            # 写请求（推送）也一样，不算在 retries 里；新连接上断掉的照旧不重发，免得重复推送。
            if transport.died_while_idle(e) and not stale_retried:
                stale_retried = fresh = True
                continue
            if attempt < retries and not isinstance(getattr(e, "reason", e), ssl.SSLCertVerificationError):
                attempt += 1
                if not getattr(e, "reused_connection", False):
                    time.sleep(1.5 * attempt)
                continue
            _record(group, time.perf_counter() - started, 0, getattr(e, "network_timing", {}), type(e).__name__)
            raise _unreachable(e, url, direct) from e
        fresh = False
        _dump(url, result)
        if (result.status >= 500 and attempt < retries and not result.retry_after()
                and not _looks_blocked(result.body[:3000].decode("utf-8", "ignore"))):
            attempt += 1
            time.sleep(1.5 * attempt)
            continue
        result.network_timing = {**timing, "elapsed": time.perf_counter() - started, "attempts": sent}
        _record(group, result.network_timing["elapsed"], result.status, timing)
        return result


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
