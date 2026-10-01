"""扫描循环。

每一轮：按网站分组，各网站各开一个线程（同一个网站内部串行、请求之间留间隔），
把每个关注的搜索结果和数据库里"见过的"比对，没见过的就是上新，价格比上次低的就是降价。

一个关注在一个网站第一次扫描时只"记住"现有商品、不推送——不然一加关注就刷屏一百条。
关键词或价格区间改了也会重新记一遍，理由相同。
"""

from __future__ import annotations

import collections
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

from . import access, net, notify, pages
from .config import ConfigStore
from .notify import Ctx, Hit
from .sources import SOURCES, Item, NoResults, Query
from .store import Store

RATE_URL = "https://open.er-api.com/v6/latest/JPY"
RATE_TTL = 12 * 3600


def norm(s: str) -> str:
    """全角半角、大小写统一后再比较：ＡＢＣ = abc，ﾊﾞｯｼﾞ = バッジ。"""
    return unicodedata.normalize("NFKC", s or "").casefold()


def alternatives(word: str) -> list[str]:
    """"fishmans|フィッシュマンズ" 这样用竖线隔开的，任一个出现就算。"""
    return [a.strip() for a in re.split(r"[|｜]", word) if a.strip()]


def _has(title: str, word: str) -> bool:
    alts = alternatives(norm(word))
    return not alts or any(a in title for a in alts)


def passes(watch: dict, item: Item) -> bool:
    if item.price is not None:
        if watch["price_min"] and item.price < watch["price_min"]:
            return False
        if watch["price_max"] and item.price > watch["price_max"]:
            return False
    title = norm(item.title)
    if not all(_has(title, w) for w in watch["must"]):
        return False
    if any(alternatives(w) and _has(title, w) for w in watch["exclude"]):
        return False
    return True


def query_of(watch: dict) -> Query:
    return Query(keyword=watch["keyword"], price_min=watch["price_min"], price_max=watch["price_max"],
                 exclude=[a for w in watch["exclude"] for a in alternatives(w)])


def query_sig(watch: dict, source: str = "") -> str:
    """决定"搜索结果集合"的那些条件。它们变了，就得重新记基线。"""
    sig = "|".join([norm(watch["keyword"]), str(watch["price_min"]), str(watch["price_max"]),
                    ",".join(sorted(norm(w) for w in watch["exclude"]))])
    rev = getattr(SOURCES.get(source), "rev", 1)
    # rev 为 1 时不写进去，老用户已经记好的基线不受影响
    return sig if rev <= 1 else f"{sig}|rev{rev}"


class Monitor:
    def __init__(self, cfgstore: ConfigStore, store: Store, echo: bool = True, serving: bool = False):
        self.cfgstore = cfgstore
        self.store = store
        self.echo = echo
        self.serving = serving    # 网页服务开着才生成推送页；once、命令行测试推送时没人提供页面
        self.busy = False
        self.last_cycle_at = 0
        self.next_cycle_at = 0
        self.logs: collections.deque = collections.deque(maxlen=200)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cycle_lock = threading.Lock()
        self._last_request: dict[str, float] = {}

    # —— 日志 ——
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.logs.append(line)
        if self.echo:
            print(line, flush=True)

    # —— 汇率 ——
    def rate(self, cfg: dict, fetch: bool = True) -> float:
        if not cfg["auto_rate"]:
            return cfg["jpy_to_cny"]
        cached = self.store.kv_get("rate")
        at = float(self.store.kv_get("rate_at", "0") or 0)
        if (cached and time.time() - at < RATE_TTL) or not fetch:
            return float(cached) if cached else cfg["jpy_to_cny"]
        try:
            value = float(net.get(RATE_URL, timeout=10).check("汇率接口").json()["rates"]["CNY"])
            self.store.kv_set("rate", str(value))
            self.store.kv_set("rate_at", str(time.time()))
            return value
        except Exception as e:
            # 一小时后再试
            self.store.kv_set("rate_at", str(time.time() - RATE_TTL + 3600))
            self.log(f"自动汇率获取失败（{e}），先用设置里的 {cfg['jpy_to_cny']}")
            return float(cached) if cached else cfg["jpy_to_cny"]

    def ctx(self, cfg: dict, hits: list[Hit] | None = None) -> Ctx:
        """推送用的上下文。给了 hits、手机又连得上本程序时，顺手把这一轮存成推送页。"""
        rate = self.rate(cfg)
        base = access.phone_base(cfg) if self.serving else ""
        page_url = ""
        if hits and base and cfg["web"]["push_page"]:
            try:
                page_url = f"{base}/p/{self.store.add_page(pages.snapshot(hits, rate))}"
            except Exception as e:   # 推送页存不下来也照样推，链接退回原商品页
                self.log(f"推送页没存下来：{e!r}")
        return Ctx(rate=rate, web_url=base, page_url=page_url)

    # —— 测试推送 ——
    def test_hits(self, cfg: dict) -> tuple[list[Hit], str]:
        """测试推送尽量用真实商品，封面和链接才和正式推送一模一样。

        先拿最近一条上新；还没有的话，用第一个关注在它的第一个网站现搜一件；
        都拿不到才退回示例。返回 (命中, 给人看的来源说明)。
        """
        rows = self.store.feed(limit=1)
        if rows:
            r = rows[0]
            item = Item(source=r["source"], id=r["item_id"], title=r["title"], price=r["price"],
                        url=r["url"], image=r["image"] or "", extra=r["extra"] or "")
            return ([Hit(item, r["watch_id"], f"测试·{r['watch_name']}", r["kind"], r["old_price"])],
                    "用的是最近一条上新")
        net.set_proxy(cfg["proxy"])
        w = next((w for w in cfg["watches"] if w["enabled"] and w["keyword"] and w["sources"]), None)
        if w is not None:
            src = SOURCES[w["sources"][0]]
            try:
                with src.lock:
                    items = src.search(query_of(w))
            except Exception as e:
                self.log(f"测试推送想现搜一件真实商品，{src.name} 没搜到：{e}")
                items = []
            live = [it for it in items if not it.sold]
            pick = next((it for it in live if passes(w, it)), live[0] if live else None)
            if pick is not None:
                return [Hit(pick, w["id"], f"测试·{w['name']}")], f"用的是刚从{src.name}搜到的一件"
        return notify.sample_hits(), "还没抓到过真实商品，发的是没有封面的示例"

    # —— 循环 ——
    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception as e:  # 循环本身不能死
                self.log(f"本轮出错：{e!r}")
            interval = self.cfgstore.load()["interval"]
            wait = interval * random.uniform(0.9, 1.1)
            self.next_cycle_at = time.time() + wait
            self._wake.wait(wait)
            self._wake.clear()

    def trigger(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run_cycle(self) -> list[Hit]:
        with self._cycle_lock:
            self.busy = True
            try:
                return self._cycle()
            finally:
                self.busy = False
                self.last_cycle_at = time.time()

    def _cycle(self) -> list[Hit]:
        cfg = self.cfgstore.load()
        net.set_proxy(cfg["proxy"])
        watches = [w for w in cfg["watches"] if w["enabled"] and w["keyword"]]
        if not watches:
            return []
        self.rate(cfg)   # 有缓存，12 小时才真去取一次
        by_source: dict[str, list] = {}
        for w in watches:
            for key in w["sources"]:
                if key in SOURCES:
                    by_source.setdefault(key, []).append(w)

        found: list[Hit] = []
        with ThreadPoolExecutor(max_workers=max(1, len(by_source))) as ex:
            for hits in ex.map(lambda kv: self._scan_source(kv[0], kv[1], cfg), by_source.items()):
                found.extend(hits)

        fresh: list[Hit] = []
        keys = set()
        blocked = self.store.blocked_keys()
        for h in found:
            k = (h.item.source, h.item.id, h.kind)
            if (h.item.source, h.item.id) in blocked:   # 用户屏蔽过的，降价了也不推
                continue
            if k in keys or self.store.already_pushed(h.item.source, h.item.id, h.kind, h.item.price):
                continue
            keys.add(k)
            self.store.add_feed(h)
            fresh.append(h)

        if fresh:
            self.log(notify.summary_title(fresh))
            channels = [c for c in cfg["channels"] if c.get("enabled", True)]
            if channels:
                for name, err in notify.send_all(channels, fresh, self.ctx(cfg, fresh)):
                    self.log(f"推送失败 · {name}：{err}")
        self.store.prune()
        return fresh

    def _polite_wait(self, key: str, gap: float) -> None:
        last = self._last_request.get(key, 0)
        wait = gap * random.uniform(0.8, 1.3) - (time.time() - last)
        if wait > 0:
            self._stop.wait(wait)

    def _scan_source(self, key: str, watches: list, cfg: dict) -> list[Hit]:
        src = SOURCES[key]
        hits: list[Hit] = []
        for w in watches:
            if self._stop.is_set():
                break
            self._polite_wait(key, cfg["request_gap"])
            try:
                with src.lock:
                    items = src.search(query_of(w))
            except Exception as e:
                msg = str(e) or repr(e)
                self.store.set_status(w["id"], key, False, 0, msg[:300])
                self.log(f"{src.name} · {w['name']}：{msg}")
                continue
            finally:
                self._last_request[key] = time.time()
            hits.extend(self.process(w, key, items))
        return hits

    def process(self, w: dict, key: str, items: list[Item]) -> list[Hit]:
        sig = query_sig(w, key)
        first = self.store.baseline_sig(w["id"], key) != sig
        known = self.store.seen_prices(w["id"], key, [it.id for it in items])
        hits: list[Hit] = []
        if not first:
            for it in items:
                if it.sold or not passes(w, it):
                    continue
                if it.id not in known:
                    hits.append(Hit(it, w["id"], w["name"], "new"))
                elif w["price_drop"]:
                    old = known[it.id]
                    if it.price is not None and old is not None and it.price < old:
                        hits.append(Hit(it, w["id"], w["name"], "drop", old))
        news = sum(1 for h in hits if h.kind == "new")
        # 一下子冒出大半页"新"商品，多半是网站换了排序或返回了别的页面，不是真上新。
        flood = news > 30 and news > 0.6 * len(items)
        self.store.mark_seen(w["id"], key, items)
        suspicious = not items and not isinstance(items, NoResults)
        if first and not suspicious:
            self.store.set_baseline(w["id"], key, sig)

        ok = True
        if suspicious:
            ok = False
            msg = "解析到 0 件：可能是网站改版了，用 python -m goodsmare search 看一下原始页面"
        elif first:
            msg = f"首次扫描，记下了现有的 {len(items)} 件，之后的新上架才会推送"
        elif flood:
            msg = f"一次冒出 {news} 件新商品，像是网站换了排序，这次只记下不推送"
            self.log(f"{SOURCES[key].name} · {w['name']}：{msg}")
            hits = []
        elif isinstance(items, NoResults):
            msg = "暂时没有搜索结果"
        else:
            msg = f"本次 {len(items)} 件，新的 {news} 件"
        self.store.set_status(w["id"], key, ok, len(items), msg)
        return hits
