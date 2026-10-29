"""扫描。

常驻时（run_forever）每个网站一个线程，按关注优先级分配检查机会，逾期比例高的先查，
同一个网站两次请求之间留间隔，各关注按优先级计算目标周期，并遵守网站的最短周期。被拦、连不上时这个站先歇一会儿，
连着出错越歇越久。默认新货即时推送，也可选择合并；每个推送渠道有独立发送队列。

命令行 once 和测试用 run_cycle：所有关注在所有网站上查一遍，查完一起推送。

一个关注在一个网站第一次扫描时只"记住"现有商品、不推送——不然一加关注就刷屏一百条。
关键词或价格区间改了也会重新记一遍，理由相同。程序停了很久再开时，这段时间冒出来的只推最新几件。
"""

from __future__ import annotations

import collections
import contextlib
import json
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

from . import access, net, notify, pages
from .config import ConfigStore, MIN_INTERVAL, PRIORITIES
from .dispatch import Dispatcher
from .notify import Ctx, Hit
from .sources import SOURCES, Item, NoResults, Query
from .sources.xianyu import account as xianyu_account
from .store import Store

RATE_URL = "https://open.er-api.com/v6/latest/JPY"
RATE_TTL = 12 * 3600
CATCHUP_GAP = 6 * 3600     # 上次查这个关注已经是 6 小时前（程序停过）：这次查到的新货算补的
CATCHUP_PUSH = 5           # 补的只推最新几件，其余的放进上新页
CATCHUP_WINDOW = 600       # 这段时间里陆续查到的补货算同一次「停过一阵」，合用上面这几个名额
DROP_RATIO = 0.9           # 比上次便宜一成以上才算降价，卖家每次减个一两百日元不提醒
PUSH_QUIET = 3             # 查到新货后再等几秒，别的网站一起冒出来的并成一条推送
PUSH_MAX_WAIT = 12         # 最多等这么久
BACKOFF = (60, 120, 300, 600, 900)   # 一个网站出错后歇多久（秒），连着出错越歇越久
PRUNE_EVERY = 600


def norm(s: str) -> str:
    """全角半角、大小写统一后再比较：ＡＢＣ = abc，ﾊﾞｯｼﾞ = バッジ。"""
    return unicodedata.normalize("NFKC", s or "").casefold()


def alternatives(word: str) -> list[str]:
    """"fishmans|フィッシュマンズ" 这样用竖线隔开的，任一个出现就算。"""
    return [a.strip() for a in re.split(r"[|｜]", word) if a.strip()]


def _has(title: str, word: str) -> bool:
    alts = alternatives(norm(word))
    return not alts or any(a in title for a in alts)


def yen_price(item: Item, rate: float) -> int | None:
    """关注里的价格区间是日元；闲鱼标的是人民币，按汇率换成日元再比。汇率不知道就不比价格。"""
    if item.price is None or getattr(SOURCES.get(item.source), "currency", "JPY") == "JPY":
        return item.price
    return round(item.price / rate) if rate else None


def passes(watch: dict, item: Item, rate: float = 0) -> bool:
    price = yen_price(item, rate)
    if price is not None:
        if watch["price_min"] and price < watch["price_min"]:
            return False
        if watch["price_max"] and price > watch["price_max"]:
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


def site_gap(cfg: dict, key: str) -> float:
    """同一个网站两次请求至少隔几秒：设置里的值，有的网站自己要求更慢。"""
    return max(cfg["request_gap"], getattr(SOURCES.get(key), "min_gap", 0))


def estimate_period(cfg: dict) -> int:
    """常驻时一个关注大概多少秒查一次：关注多的网站轮一圈要「关注数 × 请求间隔」，取最慢的那个站。

    闲鱼这种自己限了频率的站不算在里面，免得一个站把整体说慢了。"""
    periods = [period for key, src in SOURCES.items() if not src.min_interval
               for period in watch_periods(cfg, key).values()]
    return int(max(periods, default=0) + 0.5)


def watch_periods(cfg: dict, key: str) -> dict[str, float]:
    """按权重分配原有请求预算；优先级相同时与原调度一致。"""
    watches = [w for w in cfg["watches"] if w["enabled"] and w["keyword"] and key in w["sources"]]
    if not watches:
        return {}
    weights = {w["id"]: PRIORITIES.get(w.get("priority"), 1) for w in watches}
    base = max(cfg["interval"], SOURCES[key].min_interval, len(watches) * site_gap(cfg, key))
    share = base * sum(weights.values()) / len(watches)
    return {wid: max(MIN_INTERVAL, SOURCES[key].min_interval, share / weight) for wid, weight in weights.items()}


class CoolingDown(net.FetchError):
    """本地保护期，不算新的平台错误，也不延长已有的冷却。"""


class _Gate:
    """好几个网站可以同时在查；迁移数据时要等正在查的查完，并且先不让新的开始。"""

    def __init__(self):
        self._cond = threading.Condition()
        self._active = 0
        self._closed = False

    def __enter__(self):
        with self._cond:
            while self._closed:
                self._cond.wait()
            self._active += 1

    def __exit__(self, *exc):
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    @contextlib.contextmanager
    def closed(self, timeout: float):
        deadline = time.monotonic() + timeout
        with self._cond:
            if not self._cond.wait_for(lambda: not self._closed, timeout):
                raise TimeoutError
            self._closed = True
            if not self._cond.wait_for(lambda: self._active == 0, max(0, deadline - time.monotonic())):
                self._closed = False
                self._cond.notify_all()
                raise TimeoutError
        try:
            yield
        finally:
            with self._cond:
                self._closed = False
                self._cond.notify_all()


class Monitor:
    def __init__(self, cfgstore: ConfigStore, store: Store, echo: bool = True, serving: bool = False):
        self.cfgstore = cfgstore
        self.store = store
        self.echo = echo
        self.serving = serving    # 网页服务开着才生成推送页；once、命令行测试推送时没人提供页面
        self.busy = False         # run_cycle 正在跑（命令行 once、测试）
        self.live = False         # run_forever 开着：每个网站各自轮着查
        self.period = 0           # 常驻时一个关注大概多少秒查一次，网页上显示
        self.last_check_at = 0.0
        self.last_cycle_at = 0
        self.next_cycle_at = 0
        self.logs: collections.deque = collections.deque(maxlen=200)
        self._log_lock = threading.Lock()
        self._schedule_lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._gate = _Gate()
        self._last_request: dict[str, float] = {}
        self._last_scan: dict[tuple, float] = {}   # (关注, 网站) 上次查的时间
        self._backoff_until: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._backoff_kind: dict[str, str] = {}
        self._login_required: set[str] = set()
        self._requested: set[tuple[str, str]] = set()
        self._scan_times: collections.deque = collections.deque(maxlen=100)
        self._site_wake: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._queue: list[tuple[float, Hit]] = []
        self._qlock = threading.Lock()
        self._push_event = threading.Event()
        self._scans_done = threading.Event()
        self._deliver_lock = threading.Lock()
        self._catchup_left = CATCHUP_PUSH          # 这次「停过一阵」还能推几件补的
        self._catchup_until = 0.0
        self._unreachable: list[str] = []          # run_cycle 这一轮连不上的网站域名
        self._hint_at = 0.0
        self.proxy_hint = ""                       # 直连连不上时，在本机找到的能用的代理
        self._dispatcher = Dispatcher(self._gate, self.log)
        self._restore_cooldowns()
        for wid, statuses in store.statuses().items():
            for key, status in statuses.items():
                self._last_scan[(wid, key)] = status["checked_at"]
        xianyu_account.attach(store)               # 闲鱼的登录态存在数据库里

    # —— 日志 ——
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        with self._log_lock:
            self.logs.append(line)
        if self.echo:
            print(line, flush=True)

    def log_snapshot(self):
        with self._log_lock:
            return list(self.logs)[-60:]

    def timing_snapshot(self):
        with self._schedule_lock:
            scans = sorted(self._scan_times)
            cooldowns = {k: {"until": self._backoff_until.get(k, 0), "kind": self._backoff_kind.get(k, ""),
                             "login_required": k in self._login_required}
                         for k in self._backoff_until.keys() | self._login_required
                         if self._backoff_until.get(k, 0) > time.time() or k in self._login_required}
        with self._qlock:
            queued = len(self._queue)
        return {"scan_median": round(scans[len(scans) // 2], 2) if scans else None,
                "scan_samples": len(scans), "queued_hits": queued, "cooldowns": cooldowns,
                **self._dispatcher.snapshot()}

    # —— 汇率 ——
    def rate(self, cfg: dict, fetch: bool = True) -> float:
        if not cfg["auto_rate"]:
            return cfg["jpy_to_cny"]
        cached = self.store.kv_get("rate")
        at = float(self.store.kv_get("rate_at", "0") or 0)
        if (at and time.time() - at < RATE_TTL) or not fetch:
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
        rate = self.rate(cfg, fetch=False)  # 推送不能被汇率接口的超时拖住
        base = access.phone_base(cfg) if self.serving else ""
        page_url = ""
        if hits and base and cfg["web"]["push_page"]:
            try:
                page_url = f"{base}/p/{self.store.add_page(pages.snapshot(hits, rate, cfg['buy']))}"
            except Exception as e:   # 推送页存不下来也照样推，链接退回原商品页
                self.log(f"推送页没存下来：{e!r}")
        return Ctx(rate=rate, web_url=base, page_url=page_url, buy=cfg["buy"], message=cfg["message"],
                   keywords={w["id"]: w["keyword"] for w in cfg["watches"]},
                   queries={w["id"]: query_of(w) for w in cfg["watches"]})

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
        net.configure(cfg)
        w = next((w for w in cfg["watches"] if w["enabled"] and w["keyword"] and w["sources"]), None)
        if w is not None:
            src = SOURCES[w["sources"][0]]
            try:
                with self._gate:
                    items = self.search(src.key, query_of(w), cfg)
            except Exception as e:
                self.log(f"测试推送想现搜一件真实商品，{src.name} 没搜到：{e}")
                items = []
            live = [it for it in items if not it.sold]
            rate = self.rate(cfg, fetch=False)
            pick = next((it for it in live if passes(w, it, rate)), live[0] if live else None)
            if pick is not None:
                return [Hit(pick, w["id"], f"测试·{w['name']}")], f"用的是刚从{src.name}搜到的一件"
        return notify.sample_hits(), "还没抓到过真实商品，发的是没有封面的示例"

    # —— 常驻 ——
    def run_forever(self) -> None:
        """每个网站一个线程轮着查，另有一个线程管推送；这里每隔几秒看看配置，该开的线程开起来。"""
        self.live = True
        push_thread = threading.Thread(target=self._push_loop, name="push", daemon=True)
        push_thread.start()
        try:
            self._run_scheduler()
        finally:
            self.stop()
            for thread in self._threads.values():
                thread.join()
            self._scans_done.set()
            self._push_event.set()
            push_thread.join()
            self._dispatcher.close()
            self.live = False

    def _run_scheduler(self):
        last_prune = 0.0
        while not self._stop.is_set():
            try:
                cfg = self.cfgstore.load()
                net.configure(cfg)
                self.rate(cfg)   # 有缓存，12 小时才真去取一次
                wanted = {k for w in cfg["watches"] if w["enabled"] and w["keyword"]
                          for k in w["sources"] if k in SOURCES}
                for key in wanted:
                    t = self._threads.get(key)
                    if t is None or not t.is_alive():
                        self._site_wake.setdefault(key, threading.Event())
                        t = threading.Thread(target=self._site_loop, args=(key,), name=f"site-{key}", daemon=True)
                        self._threads[key] = t
                        t.start()
                self.period = estimate_period(cfg)
                if net.proxy_info()["mode"] != "direct":
                    self.proxy_hint = ""
                if time.time() - last_prune > PRUNE_EVERY:
                    with self._gate:
                        self.store.prune()
                    last_prune = time.time()
            except Exception as e:  # 调度本身不能死
                self.log(f"调度出错：{e!r}")
            self._wake.wait(5)
            self._wake.clear()
    def _pick_watch(self, key: str, watches: list, cfg: dict, now: float):
        periods = watch_periods(cfg, key)
        with self._schedule_lock:
            due = []
            for w in watches:
                pair = (w["id"], key)
                last = self._last_scan.get(pair, 0)
                period = periods[w["id"]]
                wait = max(0, last + (SOURCES[key].min_interval if pair in self._requested else period) - now)
                # 超时比例决定顺序，避免请求拥堵时低优先级永远轮不到。
                score = (now - last) / period if last else float("inf")
                due.append((wait, -score, -PRIORITIES.get(w.get("priority"), 1), w))
            chosen = min(due, key=lambda row: row[:3])
            return chosen[3], chosen[0]

    def _site_loop(self, key: str) -> None:
        src = SOURCES[key]
        wake = self._site_wake[key]
        while not self._stop.is_set():
            try:
                cfg = self.cfgstore.load()
                watches = [w for w in cfg["watches"] if w["enabled"] and w["keyword"] and key in w["sources"]]
                if not watches:
                    return    # 没有关注用这个站了；以后又有了，调度会再开一个线程
                now = time.time()
                with self._schedule_lock:
                    rest = 30 if key in self._login_required else self._backoff_until.get(key, 0) - now
                if rest > 0:
                    wake.wait(min(rest, 30))
                    wake.clear()
                    continue
                w, due = self._pick_watch(key, watches, cfg, now)
                if due > 0:
                    wake.wait(min(due, 30))
                    wake.clear()
                    continue
                if self._stop.is_set():
                    break
                with self._gate:
                    hits, err = self._scan_one(key, w, watches, cfg, self.rate(cfg, fetch=False))
                if isinstance(err, net.Unreachable):
                    self._note_unreachable(err.host)
                if hits:
                    self._enqueue(hits)
            except Exception as e:   # 一个网站的线程出意外，歇一会儿再来，别的网站照常
                self.log(f"{src.name} 的扫描出错：{e!r}")
                self._stop.wait(30)

    def _restore_cooldowns(self):
        try:
            saved = json.loads(self.store.kv_get("monitor_cooldowns", "{}"))
            for key, data in saved.items():
                if key not in SOURCES:
                    continue
                until = float(data["until"])
                kind = str(data["kind"])
                if until > time.time() or kind == "login":
                    self._backoff_until[key] = until
                    self._backoff_kind[key] = kind
                    self._fails[key] = int(data["fails"])
                    if kind == "login":
                        self._login_required.add(key)
        except (ValueError, TypeError, KeyError, AttributeError):
            pass

    def _save_cooldowns(self):
        self.store.kv_set("monitor_cooldowns", json.dumps({
            key: {"until": until, "kind": self._backoff_kind.get(key, ""), "fails": self._fails.get(key, 0)}
            for key, until in self._backoff_until.items()}))

    def _backoff(self, key: str, error: Exception) -> None:
        with self._schedule_lock:
            n = self._fails.get(key, 0) + 1
            self._fails[key] = n
            wait = max(BACKOFF[min(n, len(BACKOFF)) - 1], getattr(error, "retry_after", 0))
            self._backoff_until[key] = time.time() + wait
            self._backoff_kind[key] = getattr(error, "kind", "error")
            if self._backoff_kind[key] == "login":
                self._login_required.add(key)
            self._save_cooldowns()
        if key in self._login_required:
            self.log(f"{SOURCES[key].name} 已暂停检查，重新登录后恢复")
        elif n > 1 or wait > BACKOFF[0]:
            self.log(f"{SOURCES[key].name} 连着出错 {n} 次，先歇 {int(wait)} 秒再试")

    def search(self, key: str, query: Query, cfg: dict, watch_id: str = "") -> list[Item]:
        """扫描、预览和测试搜索共用间隔与冷却；锁内等待，避免两个入口挤在一起。"""
        src = SOURCES[key]
        with src.lock:
            with self._schedule_lock:
                if key in self._login_required:
                    raise CoolingDown(f"{src.name} 已暂停检查，请先重新登录")
                rest = self._backoff_until.get(key, 0) - time.time()
                if rest > 0:
                    raise CoolingDown(f"{src.name} 正在冷却，请在 {int(rest) + 1} 秒后重试")
            self._polite_wait(key, site_gap(cfg, key))
            if self._stop.is_set():
                raise CoolingDown("程序正在停止")
            started = time.time()
            with self._schedule_lock:
                if watch_id:
                    self._last_scan[(watch_id, key)] = started
                    self._requested.discard((watch_id, key))
            try:
                with net.route(key):
                    items = src.search(query)
            except Exception as error:
                self._backoff(key, error)
                raise
            else:
                with self._schedule_lock:
                    if key in self._fails or key in self._backoff_until:
                        self._fails.pop(key, None)
                        self._backoff_until.pop(key, None)
                        self._backoff_kind.pop(key, None)
                        self._save_cooldowns()
                return items
            finally:
                with self._schedule_lock:
                    self._last_request[key] = self.last_check_at = time.time()
                    self._scan_times.append(time.time() - started)

    def _note_unreachable(self, host: str) -> None:
        """直连连不上：看看本机有没有开着的代理软件，网页上好提示。5 分钟最多找一次。"""
        if host and net.proxy_info()["mode"] == "direct" and time.time() - self._hint_at > 300:
            self._hint_at = time.time()
            self.proxy_hint = net.find_local_proxy(host)

    def _enqueue(self, hits: list[Hit]) -> None:
        with self._qlock:
            self._queue.extend((time.monotonic(), h) for h in hits)
        self._push_event.set()

    def _take_pending(self, cfg: dict, flush: bool = False) -> list[Hit]:
        with self._qlock:
            if not self._queue:
                return []
            now = time.monotonic()
            mode = cfg["push_mode"]
            ready = (flush or mode == "instant" or now - self._queue[-1][0] >= PUSH_QUIET
                     or now - self._queue[0][0] >= PUSH_MAX_WAIT)
            priority = {w["id"] for w in cfg["watches"] if w["priority"] == "high"}
            batch, remaining = [], []
            for queued_at, h in self._queue:
                if ready or (mode == "priority" and h.watch_id in priority and not h.quiet):
                    batch.append(h)
                else:
                    remaining.append((queued_at, h))
            self._queue = remaining
            return batch

    def _push_loop(self) -> None:
        while True:
            self._push_event.wait(1)
            self._push_event.clear()
            try:
                batch = self._take_pending(self.cfgstore.load(), flush=self._stop.is_set())
                if not batch:
                    if self._stop.is_set() and (self._scans_done.is_set() or not self.live):
                        return
                    continue
                with self._gate:
                    self.deliver(batch)
            except Exception as e:
                self.log(f"推送出错：{e!r}")

    def wake(self) -> None:
        for ev in list(self._site_wake.values()):
            ev.set()
        self._wake.set()
        self._push_event.set()

    def trigger(self, watch_id: str | None = None) -> None:
        """请求提前检查；保留真实扫描时间、网站最短周期和错误冷却。"""
        cfg = self.cfgstore.load()
        with self._schedule_lock:
            self._requested.update((w["id"], key) for w in cfg["watches"]
                                   if w["enabled"] and (watch_id is None or w["id"] == watch_id)
                                   for key in w["sources"])
        self.wake()

    def rescan_source(self, key: str) -> None:
        """忘掉这个站的「上次查过」，马上查：比如闲鱼刚登录好。"""
        with self._schedule_lock:
            # 登录恢复也不能覆盖平台明确要求等待的限流期。
            if self._backoff_kind.get(key) != "login" and self._backoff_until.get(key, 0) > time.time():
                return
            for k in [k for k in self._last_scan if k[1] == key]:
                self._last_scan.pop(k, None)
            self._backoff_until.pop(key, None)
            self._fails.pop(key, None)
            self._backoff_kind.pop(key, None)
            self._login_required.discard(key)
            self._save_cooldowns()
        self.wake()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._push_event.set()
        for ev in list(self._site_wake.values()):
            ev.set()

    @contextlib.contextmanager
    def paused(self, timeout: float = 90):
        """迁移数据时用：等正在查的查完，迁移期间不开始新的。等太久抛 TimeoutError。"""
        with self._gate.closed(timeout):
            yield

    # —— 查一遍（命令行 once、测试） ——
    def run_cycle(self) -> list[Hit]:
        with self._gate:
            self.busy = True
            try:
                found = self._cycle()
                fresh = self.deliver(found)
                self.store.prune()
                return fresh
            finally:
                self.busy = False
                self.last_cycle_at = time.time()

    def _cycle(self) -> list[Hit]:
        cfg = self.cfgstore.load()
        net.configure(cfg)
        watches = [w for w in cfg["watches"] if w["enabled"] and w["keyword"]]
        if not watches:
            return []
        rate = self.rate(cfg)   # 有缓存，12 小时才真去取一次
        self._unreachable = []
        by_source: dict[str, list] = {}
        for w in watches:
            for key in w["sources"]:
                if key in SOURCES:
                    by_source.setdefault(key, []).append(w)

        found: list[Hit] = []
        with ThreadPoolExecutor(max_workers=max(1, len(by_source))) as ex:
            for hits in ex.map(lambda kv: self._scan_source(kv[0], kv[1], cfg, rate), by_source.items()):
                found.extend(hits)
        # 直连连不上（国内直连煤炉、雅虎拍卖就这样）：看看本机有没有开着的代理软件，网页上好提示
        hosts = [h for h in self._unreachable if h]
        self.proxy_hint = net.find_local_proxy(hosts[0]) if hosts and net.proxy_info()["mode"] == "direct" else ""
        return found

    def _polite_wait(self, key: str, gap: float) -> None:
        last = self._last_request.get(key, 0)
        wait = gap * random.uniform(1.0, 1.3) - (time.time() - last)
        if wait > 0:
            self._stop.wait(wait)

    def _scan_source(self, key: str, watches: list, cfg: dict, rate: float = 0) -> list[Hit]:
        src = SOURCES[key]
        hits: list[Hit] = []
        for w in watches:
            if self._stop.is_set():
                break
            with self._schedule_lock:
                if src.min_interval and time.time() - self._last_scan.get((w["id"], key), 0) < src.min_interval:
                    continue
            found, err = self._scan_one(key, w, watches, cfg, rate)
            hits.extend(found)
            if err is not None:
                break    # 这一轮连不上这个站，后面的关注已经一起标上了，别一个个再等超时
        return hits

    def _scan_one(self, key: str, w: dict, watches: list, cfg: dict, rate: float) -> tuple[list[Hit], Exception | None]:
        """查一个关注在一个网站上的新货。返回 (命中, 出的错)。"""
        src = SOURCES[key]
        try:
            items = self.search(key, query_of(w), cfg, w["id"])
        except CoolingDown as e:
            return [], e
        except Exception as e:
            msg = (str(e) or repr(e))[:300]
            kind = getattr(e, "kind", "error")
            self.store.set_status(w["id"], key, False, 0, msg, kind)
            self.log(f"{src.name} · {w['name']}：{msg}")
            if isinstance(e, net.Unreachable) or kind == "login":
                if isinstance(e, net.Unreachable):
                    self._unreachable.append(e.host)
                for other in watches:   # 这个站连不上，用它的关注都标上
                    if other["id"] != w["id"]:
                        self.store.set_status(other["id"], key, False, 0, msg, kind)
            return [], e
        return self.process(w, key, items, rate), None

    def process(self, w: dict, key: str, items: list[Item], rate: float = 0) -> list[Hit]:
        sig = query_sig(w, key)
        first = self.store.baseline_sig(w["id"], key) != sig
        prev = self.store.status_of(w["id"], key)
        # 上次查这个关注是好几个小时前（程序停过）：这次冒出来的是这段时间攒下的，算补的
        catchup = prev is not None and time.time() - prev["checked_at"] > CATCHUP_GAP
        known = self.store.seen_prices(w["id"], key, [it.id for it in items])
        hits: list[Hit] = []
        if not first:
            for it in items:
                if it.sold or not passes(w, it, rate):
                    continue
                if it.id not in known:
                    hits.append(Hit(it, w["id"], w["name"], "new", quiet=catchup))
                elif w["price_drop"]:
                    old = known[it.id]
                    if it.price is not None and old is not None and it.price <= old * DROP_RATIO:
                        hits.append(Hit(it, w["id"], w["name"], "drop", old, quiet=catchup))
        news = sum(1 for h in hits if h.kind == "new")
        # 一下子冒出大半页"新"商品，多半是网站换了排序或返回了别的页面，不是真上新。补的不算。
        flood = not catchup and news > 30 and news > 0.6 * len(items)
        self.store.mark_seen(w["id"], key, items)
        suspicious = not items and not isinstance(items, NoResults)
        if first and not suspicious:
            self.store.set_baseline(w["id"], key, sig)

        ok, kind = True, ""
        if suspicious:
            ok, kind = False, "parse"
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
        self.store.set_status(w["id"], key, ok, len(items), msg, kind)
        return hits

    # —— 推送 ——
    def deliver(self, found: list[Hit]) -> list[Hit]:
        """把查到的命中去掉屏蔽的、推过的、卖家删了重发的，记进上新，再推送。返回推出去的。"""
        if not found:
            return []
        with self._deliver_lock:
            cfg = self.cfgstore.load()
            fresh: list[Hit] = []
            quiet: list[Hit] = []
            keys = set()
            blocked = self.store.blocked_keys()
            for h in found:
                k = (h.item.source, h.item.id, h.kind)
                if (h.item.source, h.item.id) in blocked or k in keys:   # 用户屏蔽过的，降价了也不推
                    continue
                if self.store.already_pushed(h.item.source, h.item.id, h.kind, h.item.price):
                    continue
                # 同一个关注 7 天内推过标题、价格都一样的：多半是卖家删了重新上架
                if h.kind == "new" and self.store.relisted(h.watch_id, h.item.source, h.item.title, h.item.price):
                    continue
                keys.add(k)
                self.store.add_feed(h)
                (quiet if h.quiet else fresh).append(h)
            if quiet:
                # 程序停过一阵：只挑最新的几件推，其余的在网页的「上新」里。
                # 常驻时各网站陆续查完、分好几批送来，几批合用一份名额
                now = time.time()
                if now > self._catchup_until:
                    self._catchup_left = CATCHUP_PUSH
                self._catchup_until = now + CATCHUP_WINDOW
                quiet.sort(key=lambda h: h.item.created, reverse=True)
                room = min(len(quiet), max(0, min(self._catchup_left, CATCHUP_PUSH - len(fresh))))
                self._catchup_left -= room
                fresh.extend(quiet[:room])
                if len(quiet) > room:
                    self.log(f"程序停过一阵，这次补查到 {len(quiet)} 件，推了 {room} 件，其余的放在上新页")
            if fresh:
                self.log(notify.summary_title(fresh))
                channels = [c for c in cfg["channels"] if c.get("enabled", True)]
                if channels:
                    ctx = self.ctx(cfg, fresh)
                    if self.live:
                        detected_at = min(h.detected_at for h in fresh)
                        for channel in channels:
                            self._dispatcher.submit(channel, fresh, ctx, detected_at)
                    else:
                        for name, err in notify.send_all(channels, fresh, ctx):
                            self.log(f"推送失败 · {name}：{err}")
            return fresh
