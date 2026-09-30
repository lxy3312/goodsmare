"""配置：data/config.json 一个文件。网页、命令行都读写它，监控每一轮重新读，改完不用重启。"""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from pathlib import Path

ALL_SOURCES = ["mercari", "yahoo_auction", "yahoo_flea", "rakuma", "surugaya", "mandarake", "animate"]

DEFAULTS = {
    "interval": 180,        # 两轮扫描之间隔多少秒
    "request_gap": 3.0,     # 同一个网站两次请求之间至少隔多少秒，别把人家打挂、也别被封
    "proxy": "",            # 例如 http://127.0.0.1:7890 ；留空则用系统环境变量
    "jpy_to_cny": 0.048,    # 自动汇率拿不到时用这个
    "auto_rate": True,
    "web": {"host": "127.0.0.1", "port": 8787, "token": ""},
    "channels": [],
    "watches": [],
}

WATCH_DEFAULTS = {
    "id": "",
    "name": "",
    "keyword": "",
    "sources": list(ALL_SOURCES),
    "price_min": 0,
    "price_max": 0,
    "must": [],             # 标题里必须全部出现的词（煤炉搜索很模糊，靠这个过滤）；"A|B" 表示任一个
    "exclude": [],          # 标题里出现任何一个就不要
    "price_drop": False,    # 已经见过的商品降价了也提醒
    "enabled": True,
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
    except (TypeError, ValueError):
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
    return out


def normalize_channel(c: dict) -> dict:
    out = copy.deepcopy(CHANNEL_DEFAULTS)
    out.update({k: (v.strip() if isinstance(v, str) else v) for k, v in (c or {}).items()})
    out["id"] = str(out["id"] or new_id())
    out["enabled"] = bool(out.get("enabled", True))
    return out


def normalize(cfg: dict) -> dict:
    out = copy.deepcopy(DEFAULTS)
    for k, v in (cfg or {}).items():
        if k == "web" and isinstance(v, dict):
            out["web"].update(v)
        elif k in DEFAULTS:
            out[k] = v
    try:
        out["interval"] = max(30, int(float(out["interval"])))
    except (TypeError, ValueError):
        out["interval"] = DEFAULTS["interval"]
    try:
        out["request_gap"] = max(0.5, float(out["request_gap"]))
    except (TypeError, ValueError):
        out["request_gap"] = DEFAULTS["request_gap"]
    try:
        out["jpy_to_cny"] = float(out["jpy_to_cny"]) or DEFAULTS["jpy_to_cny"]
    except (TypeError, ValueError):
        out["jpy_to_cny"] = DEFAULTS["jpy_to_cny"]
    out["auto_rate"] = bool(out["auto_rate"])
    out["proxy"] = str(out["proxy"] or "").strip()
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
