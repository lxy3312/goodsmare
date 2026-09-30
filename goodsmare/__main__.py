"""命令行入口。

  python -m goodsmare                 启动：后台扫描 + 本地网页（默认 http://127.0.0.1:8787）
  python -m goodsmare once            只扫一轮就退出（给 crontab / 计划任务用）
  python -m goodsmare search 煤炉 初音ミク   试搜某个网站，看解析得对不对
  python -m goodsmare add 五条悟 缶バッジ --max 3000
  python -m goodsmare list | rm ID | test-notify | sources
"""

from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from pathlib import Path

from . import APP_NAME, __version__, net, notify
from .config import ALL_SOURCES, ConfigStore, normalize_watch
from .monitor import Monitor, passes, query_of
from .sources import SOURCES
from .store import Store

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data"

# 命令行里可以用中文名指代网站
ALIASES = {
    "煤炉": "mercari", "mercari": "mercari", "メルカリ": "mercari",
    "雅虎": "yahoo_auction", "雅虎拍卖": "yahoo_auction", "yahoo": "yahoo_auction", "ヤフオク": "yahoo_auction",
    "雅虎闲置": "yahoo_flea", "paypay": "yahoo_flea", "フリマ": "yahoo_flea", "yahoo_flea": "yahoo_flea",
    "乐天": "rakuma", "rakuma": "rakuma", "ラクマ": "rakuma", "fril": "rakuma",
    "骏河屋": "surugaya", "駿河屋": "surugaya", "surugaya": "surugaya", "suruga": "surugaya",
    "mandarake": "mandarake", "まんだらけ": "mandarake", "蔓德拉": "mandarake", "曼达拉": "mandarake",
    "animate": "animate", "アニメイト": "animate",
    "yahoo_auction": "yahoo_auction",
}


def _utf8_console() -> None:
    # Windows 老控制台默认 GBK，中文日文混排会炸
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _stores(args):
    data = Path(args.data)
    return ConfigStore(data / "config.json"), Store(data / "goodsmare.db")


def _source(name: str) -> str:
    key = ALIASES.get(name.lower(), ALIASES.get(name))
    if key is None:
        sys.exit(f"不认识的网站：{name}（可选：{'、'.join(s.name + '/' + k for k, s in SOURCES.items())}）")
    return key


def cmd_run(args):
    cfgstore, store = _stores(args)
    cfg = cfgstore.load()
    if not cfgstore.path.exists():
        cfgstore.save(cfg)
    host = args.host or cfg["web"]["host"]
    port = args.port or cfg["web"]["port"]
    if args.host or args.port:
        cfgstore.update(lambda c: c["web"].update({"host": host, "port": port}))
    if host not in ("127.0.0.1", "localhost", "::1") and not cfg["web"].get("token"):
        print("⚠ 网页对局域网开放了但没设口令：任何能连上的人都能改你的配置。"
              "建议在 data/config.json 的 web.token 里设一个。")

    from .web import App, serve
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}/"
    monitor = Monitor(cfgstore, store, web_url=url if shown not in ("127.0.0.1", "localhost") else "")
    try:
        server = serve(App(cfgstore, store, monitor), host, port)
    except OSError as e:
        sys.exit(f"端口 {port} 用不了（{e}）。换一个：python -m goodsmare --port 8788")

    print(f"{APP_NAME} v{__version__} 已启动")
    print(f"  网页：{url}")
    print(f"  数据：{Path(args.data).resolve()}")
    print("  关掉这个窗口（或按 Ctrl+C）就停止监控\n")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        monitor.run_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        monitor.stop()
        server.shutdown()


def cmd_once(args):
    cfgstore, store = _stores(args)
    monitor = Monitor(cfgstore, store)
    hits = monitor.run_cycle()
    statuses = store.statuses()
    for w in cfgstore.load()["watches"]:
        for key, st in statuses.get(w["id"], {}).items():
            print(f"  {w['name']} · {SOURCES[key].name if key in SOURCES else key}：{st['message']}")
    print(f"本轮新推送 {len(hits)} 件")


def cmd_search(args):
    cfgstore, _ = _stores(args)
    cfg = cfgstore.load()
    net.set_proxy(cfg["proxy"])
    if args.dump:
        net.dump_dir = Path(args.dump)
    key = _source(args.source)
    watch = normalize_watch({"keyword": " ".join(args.keyword), "price_min": args.min,
                             "price_max": args.max, "must": args.must or "", "exclude": args.exclude or ""})
    src = SOURCES[key]
    t = time.time()
    try:
        items = src.search(query_of(watch))
    except net.FetchError as e:
        sys.exit(f"失败：{e}")
    print(f"{src.name} · 「{watch['keyword']}」 {len(items)} 件，用时 {time.time() - t:.1f}s\n")
    for it in items[:args.limit]:
        mark = " " if passes(watch, it) else "×"
        price = "?" if it.price is None else f"{it.price:,}"
        extra = f"  [{it.extra}]" if it.extra else ""
        sold = "  (已售)" if it.sold else ""
        print(f"{mark} ¥{price:>8}  {it.title[:60]}{extra}{sold}\n             {it.url}")
    if not items:
        print("一件都没有。如果网站上明明搜得到，多半是页面改版了："
              "加上 --dump 目录 把原始页面存下来，拿去对照解析代码。")
    if args.dump:
        print(f"\n原始响应已存到 {Path(args.dump).resolve()}")


def cmd_add(args):
    cfgstore, _ = _stores(args)
    sources = [_source(s) for s in args.sources.split(",")] if args.sources else list(ALL_SOURCES)
    watch = normalize_watch({"keyword": " ".join(args.keyword), "name": args.name or "",
                             "sources": sources, "price_min": args.min, "price_max": args.max,
                             "must": args.must or "", "exclude": args.exclude or "",
                             "price_drop": args.drop})
    cfgstore.update(lambda cfg: cfg["watches"].insert(0, watch))
    print(f"已添加：{watch['name']}（{watch['id']}）")


def cmd_list(args):
    cfgstore, store = _stores(args)
    cfg = cfgstore.load()
    statuses = store.statuses()
    if not cfg["watches"]:
        print("还没有关注。python -m goodsmare add 关键词  或者在网页里加。")
    for w in cfg["watches"]:
        flag = "" if w["enabled"] else "（已暂停）"
        rng = ""
        if w["price_min"] or w["price_max"]:
            rng = f" ¥{w['price_min'] or 0}~{w['price_max'] or '不限'}"
        print(f"{w['id']}  {w['name']}{flag}  「{w['keyword']}」{rng}")
        for key in w["sources"]:
            st = statuses.get(w["id"], {}).get(key)
            print(f"    {SOURCES[key].name}: {st['message'] if st else '还没扫过'}")


def cmd_rm(args):
    cfgstore, store = _stores(args)
    before = len(cfgstore.load()["watches"])
    cfg = cfgstore.update(lambda c: c.__setitem__("watches", [w for w in c["watches"] if w["id"] != args.id]))
    store.forget_watch(args.id)
    print("已删除" if len(cfg["watches"]) < before else "没找到这个 ID")


def cmd_test_notify(args):
    cfgstore, store = _stores(args)
    cfg = cfgstore.load()
    net.set_proxy(cfg["proxy"])
    if not cfg["channels"]:
        sys.exit("还没配推送渠道，去网页的「推送」里加一个。")
    monitor = Monitor(cfgstore, store)
    ctx = monitor.ctx(cfg)
    hits, where = monitor.test_hits(cfg)
    print(f"测试内容：{where}")
    for ch in cfg["channels"]:
        name = ch.get("name") or notify.CHANNEL_TYPES.get(ch["type"], (ch["type"],))[0]
        try:
            notify.send(ch, hits, ctx)
            print(f"✓ {name}")
        except Exception as e:
            print(f"✗ {name}：{e}")


def cmd_sources(args):
    for key in ALL_SOURCES:
        print(f"{key:<14} {SOURCES[key].name:<8} {SOURCES[key].home}")


def main(argv=None):
    _utf8_console()
    p = argparse.ArgumentParser(prog="python -m goodsmare", description=f"{APP_NAME}：日淘上新提醒")
    p.add_argument("--data", default=str(DEFAULT_DATA), help="数据目录（配置和数据库放这里）")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="启动监控和网页（默认）")
    for sp, default in ((p, None), (r, argparse.SUPPRESS)):
        # 子命令里用 SUPPRESS，免得 `--port 1 run` 被子命令的默认值盖掉
        sp.add_argument("--host", default=default if default else "", help="网页监听地址，手机要看就用 0.0.0.0")
        sp.add_argument("--port", type=int, default=default if default else 0, help="网页端口，默认 8787")
        sp.add_argument("--no-browser", action="store_true", default=default if default else False,
                        help="启动时不自动打开浏览器")

    sub.add_parser("once", help="扫一轮就退出")

    s = sub.add_parser("search", help="试搜一个网站")
    s.add_argument("source", help="网站：煤炉 雅虎 雅虎闲置 乐天 骏河屋 mandarake animate")
    s.add_argument("keyword", nargs="+")
    a = sub.add_parser("add", help="加一个关注")
    a.add_argument("keyword", nargs="+")
    a.add_argument("--name", help="显示名，默认用关键词")
    a.add_argument("--sources", help="逗号分隔，默认全部")
    a.add_argument("--drop", action="store_true", help="降价也提醒")
    for sp in (s, a):
        sp.add_argument("--min", type=int, default=0, help="最低价（日元）")
        sp.add_argument("--max", type=int, default=0, help="最高价（日元）")
        sp.add_argument("--must", help="标题必须包含的词，逗号分隔；写成 A|B 是有一个就行")
        sp.add_argument("--exclude", help="标题不能包含的词，逗号分隔")
    s.add_argument("--limit", type=int, default=30, help="最多显示几条")
    s.add_argument("--dump", help="把原始响应存到这个目录")

    sub.add_parser("list", help="列出关注和状态")
    rm = sub.add_parser("rm", help="删除关注")
    rm.add_argument("id")
    sub.add_parser("test-notify", help="给所有推送渠道发一条测试")
    sub.add_parser("sources", help="支持的网站")

    args = p.parse_args(argv)
    {
        None: cmd_run, "run": cmd_run, "once": cmd_once, "search": cmd_search, "add": cmd_add,
        "list": cmd_list, "rm": cmd_rm, "test-notify": cmd_test_notify, "sources": cmd_sources,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
