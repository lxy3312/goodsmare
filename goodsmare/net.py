"""HTTP：只用标准库 urllib。代理、gzip、编码、被拦截的识别都在这里。"""

from __future__ import annotations

import gzip
import json
import re
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
_proxy = ""
_opener = urllib.request.build_opener()

# 调试用：设了目录就把每次响应原样存下来，页面改版时拿去对照。
dump_dir: Path | None = None


class FetchError(Exception):
    pass


class Blocked(FetchError):
    """对方返回了验证页 / 限流页，不是我们解析的问题。"""


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
    """proxy 为空时沿用系统环境变量（HTTPS_PROXY 等）。"""
    global _proxy, _opener
    proxy = (proxy or "").strip()
    with _lock:
        if proxy == _proxy:
            return
        _proxy = proxy
        if proxy:
            if "://" not in proxy:
                proxy = "http://" + proxy
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            _opener = urllib.request.build_opener(handler)
        else:
            _opener = urllib.request.build_opener()


def request(method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
            json_body=None, data: bytes | dict | None = None, timeout: float = 20,
            retries: int = 1) -> Response:
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
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with _opener.open(req, timeout=timeout) as resp:
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
            reason = getattr(e, "reason", e)
            raise FetchError(f"连不上 {urllib.parse.urlsplit(url).netloc}：{reason}") from e
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
