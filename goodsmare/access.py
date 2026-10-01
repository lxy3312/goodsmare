"""手机访问：网页对局域网开放、手机该用哪个地址打开、推送里的链接指向哪里。

电脑上的 goodsmare 只有手机能连上时，推送页和管理网页才在手机上打得开：
同一个 Wi-Fi 下用局域网地址；出门在外要 Tailscale、内网穿透之类，地址自己填进 web.public_url。
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
import time
import urllib.parse

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
ANY = {"0.0.0.0", "::", ""}
LAN_HOST = "0.0.0.0"

_cache: tuple[float, list] = (0.0, [])


def is_loopback(host: str) -> bool:
    return host in LOOPBACK


def new_token() -> str:
    return secrets.token_urlsafe(9)   # 12 个字符


def _kind(ip: str) -> str:
    """给地址起个人能看懂的名字；用不上的返回空。"""
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return ""
    if a in ipaddress.IPv4Network("100.64.0.0/10"):
        return "Tailscale 等"    # 运营商级 NAT 段，Tailscale、ZeroTier 常用
    if a in ipaddress.IPv4Network("198.18.0.0/15") or a.is_loopback or a.is_link_local or a.is_unspecified:
        return ""                # Clash TUN 的假 IP、回环、自动分配失败的 169.254
    if ip.split(".")[0] in ("25", "26"):
        return ""                # Hamachi、Radmin VPN 借用的网段，手机上没有这俩
    if a.is_private:
        return "局域网"
    return "公网"


def lan_ips() -> list[dict]:
    """这台电脑的 IPv4 地址，最可能是手机能连的排前面。[{ip, label}]，缓存一分钟。"""
    global _cache
    at, cached = _cache
    if time.time() - at < 60:
        return cached
    found: list[str] = []
    try:
        # 默认路由走的那块网卡：UDP 的 connect 不发包，只让系统选出本机地址
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass

    def rank(ip: str) -> int:
        kind = _kind(ip)
        if ip.startswith("192.168.56."):   # VirtualBox 的仅主机网卡
            return 5
        return {"局域网": 1, "Tailscale 等": 3, "公网": 4}.get(kind, 9)
    out, seen = [], set()
    for i, ip in enumerate(found):
        if ip in seen or not _kind(ip):
            continue
        seen.add(ip)
        out.append((rank(ip), i, ip))
    result = [{"ip": ip, "label": _kind(ip)} for _, _, ip in sorted(out)]
    _cache = (time.time(), result)
    return result


def lan_on(cfg: dict) -> bool:
    return not is_loopback(cfg["web"]["host"])


def phone_base(cfg: dict) -> str:
    """手机打开本程序用的地址，比如 http://192.168.1.5:8787；手机连不上时返回空。"""
    web = cfg["web"]
    if web.get("public_url"):
        return web["public_url"]
    if not lan_on(cfg):
        return ""
    host = web["host"]
    if host in ANY:
        ips = lan_ips()
        if not ips:
            return ""
        host = ips[0]["ip"]
    return f"http://{host}:{web['port']}"


def normalize_public_url(url: str) -> str:
    """用户填的地址：补上 http://，去掉末尾的 /；明显不对的报错。"""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    try:
        parts = urllib.parse.urlsplit(url)
        # 端口不是数字时，读 .port 会抛 ValueError
        ok = parts.scheme in ("http", "https") and bool(parts.hostname) and (parts.port or 0) >= 0
    except ValueError:
        ok = False
    if not ok or any(c.isspace() for c in url):
        raise ValueError("地址要像 http://192.168.1.5:8787 或 https://example.com 这样")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
