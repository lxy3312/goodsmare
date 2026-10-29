"""配置：data/config.json 一个文件。网页、命令行都读写它，监控每一轮重新读，改完不用重启。"""

from __future__ import annotations

import copy
import json
import math
import os
import threading
import uuid
import urllib.parse
from pathlib import Path

from . import message_layout

# 新建关注默认勾上的网站；闲鱼要先登录，用的人自己勾
DEFAULT_SOURCES = ["mercari", "yahoo_auction", "yahoo_flea", "rakuma", "surugaya", "mandarake", "animate"]
ALL_SOURCES = DEFAULT_SOURCES + ["xianyu"]

MIN_INTERVAL = 10
PRIORITIES = {"high": 2.0, "normal": 1.0, "low": 0.5}
PUSH_MODES = ("instant", "priority", "batch")
NETWORK_DEFAULTS = {"keepalive": True, "connect_timeout": 8.0, "read_timeout": 20.0,
                    "routes": {"domestic_push": "direct"}}
NETWORK_ROUTES = ALL_SOURCES + ["domestic_push", "overseas_push"]

DEFAULTS = {
    "interval": 60,         # 同一个关注在同一个网站多久查一次（秒）；关注多时还受下面的请求间隔限制
    "request_gap": 3.0,     # 同一个网站两次请求之间至少隔多少秒，别把人家打挂、也别被封
    "push_mode": "instant", # 即时推送 / 重点即时、其余合并 / 全部合并
    "buy": "",              # 推送和上新页里点商品去哪：空 = 原网页，doorzo = 任意门
    "message": None,         # 空着用各渠道原有排版；消息积木可统一配置，也可在渠道上单独覆盖
    "proxy": "",            # 例如 http://127.0.0.1:7890 ；留空则用系统环境变量
    "network": copy.deepcopy(NETWORK_DEFAULTS),
    "jpy_to_cny": 0.048,    # 自动汇率拿不到时用这个
    "auto_rate": True,
    # host 为 0.0.0.0 时对局域网开放（必须有 token）；public_url 是手机打开本程序用的地址，
    # 空着就用自动找到的局域网地址；push_page 为真时，推送点开是本程序上的推送页
    "web": {"host": "127.0.0.1", "port": 8787, "token": "", "public_url": "", "push_page": True},
    "channels": [],
    "watches": [],
}

WATCH_DEFAULTS = {
    "id": "",
    "name": "",
    "keyword": "",
    "sources": list(DEFAULT_SOURCES),
    "price_min": 0,
    "price_max": 0,
    "must": [],             # 标题里必须全部出现的词（煤炉搜索很模糊，靠这个过滤）；"A|B" 表示任一个
    "exclude": [],          # 标题里出现任何一个就不要
    "price_drop": False,    # 已经见过的商品降价了也提醒
    "enabled": True,
    "priority": "normal",  # 同站请求预算不变，重点关注分到更多检查机会
}

CHANNEL_DEFAULTS = {"id": "", "type": "", "name": "", "enabled": True}


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def _as_list(v) -> list:
    if isinstance(v, str):
        return [w for w in (s.strip() for s in v.replace("，", ",").replace("、", ",").split(",")) if w]
    if isinstance(v, (list, tuple)):
        return [str(w).strip() for w in v if str(w).strip()]
    return []


def _as_int(v) -> int:
    try:
        return max(0, int(float(v or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_watch(w: dict) -> dict:
    out = copy.deepcopy(WATCH_DEFAULTS)
    out.update({k: v for k, v in (w or {}).items() if k in WATCH_DEFAULTS})
    out["id"] = str(out["id"] or new_id())
    out["keyword"] = str(out["keyword"]).strip()
    out["name"] = str(out["name"]).strip() or out["keyword"]
    out["sources"] = [s for s in _as_list(out["sources"]) if s in ALL_SOURCES]
    out["price_min"] = _as_int(out["price_min"])
    out["price_max"] = _as_int(out["price_max"])
    out["must"] = _as_list(out["must"])
    out["exclude"] = _as_list(out["exclude"])
    out["price_drop"] = bool(out["price_drop"])
    out["enabled"] = bool(out["enabled"])
    out["priority"] = out["priority"] if isinstance(out["priority"], str) and out["priority"] in PRIORITIES else "normal"
    return out


def normalize_channel(c: dict) -> dict:
    out = copy.deepcopy(CHANNEL_DEFAULTS)
    out.update({k: (v.strip() if isinstance(v, str) else v) for k, v in (c or {}).items()})
    out["id"] = str(out["id"] or new_id())
    out["enabled"] = bool(out.get("enabled", True))
    if "message" in out:
        layout = message_layout.normalize(out["message"])
        if layout is None:
            out.pop("message")
        else:
            out["message"] = layout
    return out


def normalize_network(value) -> dict:
    out = copy.deepcopy(NETWORK_DEFAULTS)
    if not isinstance(value, dict):
        value = {}
    out["keepalive"] = bool(value.get("keepalive", True))
    for key, minimum, maximum in (("connect_timeout", 2, 30), ("read_timeout", 3, 60)):
        try:
            number = float(value.get(key, out[key]))
            if math.isfinite(number):
                out[key] = max(minimum, min(maximum, number))
        except (ValueError, TypeError, OverflowError):
            pass
    routes = value.get("routes")
    if isinstance(routes, dict):
        for key, route in routes.items():
            if key not in NETWORK_ROUTES or key == "xianyu":
                continue
            route = str(route or "inherit").strip()
            if route not in ("inherit", "direct"):
                if "://" not in route:
                    route = "http://" + route
                try:
                    parsed = urllib.parse.urlsplit(route)
                    if parsed.scheme != "http" or not parsed.hostname or not parsed.port or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                        raise ValueError
                except ValueError:
                    raise ValueError("网站专用代理请填 HTTP 代理地址和端口，例如 http://127.0.0.1:7890") from None
            out["routes"][key] = route
    out["routes"]["xianyu"] = "direct"
    return out


def normalize(cfg: dict) -> dict:
    out = copy.deepcopy(DEFAULTS)
    for k, v in (cfg or {}).items():
        if k == "web" and isinstance(v, dict):
            out["web"].update(v)
        elif k in DEFAULTS:
            out[k] = v
    try:
        out["interval"] = max(MIN_INTERVAL, int(float(out["interval"])))
    except (TypeError, ValueError, OverflowError):
        out["interval"] = DEFAULTS["interval"]
    out["push_mode"] = out["push_mode"] if out["push_mode"] in PUSH_MODES else DEFAULTS["push_mode"]
    out["buy"] = out["buy"] if out["buy"] in ("", "doorzo") else ""
    out["message"] = message_layout.normalize(out["message"])
    try:
        out["request_gap"] = max(0.5, float(out["request_gap"]))
        if not math.isfinite(out["request_gap"]):
            raise ValueError
    except (TypeError, ValueError):
        out["request_gap"] = DEFAULTS["request_gap"]
    try:
        out["jpy_to_cny"] = float(out["jpy_to_cny"]) or DEFAULTS["jpy_to_cny"]
        if not math.isfinite(out["jpy_to_cny"]) or out["jpy_to_cny"] <= 0:
            raise ValueError
    except (TypeError, ValueError):
        out["jpy_to_cny"] = DEFAULTS["jpy_to_cny"]
    out["auto_rate"] = bool(out["auto_rate"])
    out["proxy"] = str(out["proxy"] or "").strip()
    out["network"] = normalize_network(out["network"])
    web = out["web"]
    web["host"] = str(web.get("host") or "127.0.0.1").strip()
    web["token"] = str(web.get("token") or "").strip()
    web["public_url"] = str(web.get("public_url") or "").strip().rstrip("/")
    web["push_page"] = bool(web.get("push_page", True))
    out["watches"] = [normalize_watch(w) for w in out["watches"] if isinstance(w, dict)]
    out["channels"] = [normalize_channel(c) for c in out["channels"] if isinstance(c, dict)]
    return out


class ConfigStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> dict:
        with self._lock:
            if not self.path.exists():
                return normalize({})
            with open(self.path, encoding="utf-8") as f:
                return normalize(json.load(f))

    def save(self, cfg: dict) -> dict:
        cfg = normalize(cfg)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        return cfg

    def update(self, fn) -> dict:
        """读-改-写，整个过程持锁。fn 直接修改传进去的 dict。"""
        with self._lock:
            cfg = self.load()
            fn(cfg)
            return self.save(cfg)
