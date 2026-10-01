"""把旧版（改名前的 ritao）或另一份 goodsmare 的数据并进来，两边的东西都留着。

  python -m goodsmare migrate 旧文件夹            先演练：只说会怎么合并，什么都不写
  python -m goodsmare migrate 旧文件夹 --apply    真的合并；动手前先把现在的数据整份备份
网页里「设置 → 从旧版迁移」做的是同一件事。

旧文件夹给程序文件夹或者它的 data 文件夹都行。容易丢数据的几个地方：

- SQLite 开的是 WAL 模式，最近的改动常常还在旁边的 xxx.db-wal 里，没写回主文件，只拷 .db 就丢了。
  这里用 SQLite 自己的备份接口只读地取一份快照（WAL 里的也在内），取不了再把 .db、-wal、-shm
  一起拷到临时目录打开；旧文件夹里的文件都不改。
- 用命令行合并时，现在这份数据不能有程序在用（看端口上有没有程序在听）；在网页里合并时，先停下扫描再合。
- 两边各有一个关键词相同的关注（ID 不同）时认成同一个，记录都并到现在这个下面，不会多出重复的关注；
  配置一样的推送渠道也只留一个，不然会推两遍。
- 旧版解析错过的网站（Source.rev 比旧基线新），旧记录里的价格不可信，这些"见过的商品"不并，
  免得报出假降价；它们的推送记录照样并进来。
- 推送记录按时间重排，上新页里新旧记录的先后是对的；一模一样的记录不会重复导入，多跑几次也一样。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import access
from .config import ConfigStore, new_id, normalize
from .monitor import norm
from .sources import SOURCES
from .store import Store

DB_NAMES = ("goodsmare.db", "ritao.db")
SETTINGS = ("interval", "request_gap", "proxy", "jpy_to_cny", "auto_rate")
FEED_COLS = ("source", "item_id", "title", "price", "old_price", "url", "image", "extra",
             "watch_id", "watch_name", "kind", "found_at")


class MigrateError(ValueError):
    pass


def locate(path) -> tuple[Path | None, Path | None]:
    """旧文件夹里的 config.json 和数据库。"""
    p = Path(path).expanduser()
    for d in (p / "data", p):
        if not d.is_dir():
            continue
        cfg = d / "config.json"
        db = next((d / n for n in DB_NAMES if (d / n).exists()), None)
        if cfg.exists() or db:
            return (cfg if cfg.exists() else None), db
    raise MigrateError(f"{p} 里没找到 config.json，也没找到 goodsmare.db 或 ritao.db")


def snapshot(db: Path, workdir: Path) -> Path:
    """在临时目录里得到数据库的一份完整快照，WAL 里还没写回主文件的改动也在里面。

    先用 SQLite 的备份接口只读地取（那边的程序开着也能取到一致的一份）；只读打不开时
    （比如 WAL 文件在、-shm 不在），把 .db、-wal、-shm 一起拷过来，由 SQLite 自己合进去。
    """
    out = workdir / db.name
    try:
        src = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(out))
            src.backup(dst)
            dst.close()
        finally:
            src.close()
    except sqlite3.Error:
        for suffix in ("", "-wal", "-shm"):
            Path(str(out) + suffix).unlink(missing_ok=True)
            f = Path(str(db) + suffix)
            if f.exists():
                shutil.copy2(f, str(out) + suffix)
    con = sqlite3.connect(str(out))
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise MigrateError(f"{db} 有损坏（{ok}），没法合并")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError as e:
        raise MigrateError(f"{db} 打不开：{e}") from e
    finally:
        con.close()
    return out


def _rows(con: sqlite3.Connection, table: str) -> list[dict]:
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        return []
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(f"SELECT * FROM {table}")]


def _watch_key(w: dict) -> str:
    return " ".join(norm(w["keyword"]).split())


def _channel_key(c: dict) -> str:
    return json.dumps({k: v for k, v in c.items() if k not in ("id", "name", "enabled")},
                      ensure_ascii=False, sort_keys=True)


def _stale(source: str, sig: str | None) -> bool:
    """这个网站的解析改过（rev 变大），而旧基线不是新解析记的：旧价格不能信。"""
    rev = getattr(SOURCES.get(source), "rev", 1)
    return rev > 1 and not (sig or "").endswith(f"|rev{rev}")


@dataclass
class Plan:
    fresh: bool                                   # 现在这边还什么都没有
    watch_map: dict = field(default_factory=dict)  # 旧关注 ID → 合并后的关注 ID
    matched: list = field(default_factory=list)    # (旧名字, 现在的名字)
    new_watches: list = field(default_factory=list)
    new_channels: list = field(default_factory=list)
    same_channels: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)   # 两边不一样的设置 {项: (旧, 现在)}
    seen_new: list = field(default_factory=list)
    seen_update: list = field(default_factory=list)
    seen_skipped: dict = field(default_factory=dict)
    baseline_new: list = field(default_factory=list)
    feed_new: list = field(default_factory=list)
    feed_dup: int = 0
    status_new: list = field(default_factory=list)
    kv_new: list = field(default_factory=list)
    page_new: list = field(default_factory=list)
    blocked_new: list = field(default_factory=list)
    src_counts: dict = field(default_factory=dict)
    dst_counts: dict = field(default_factory=dict)

    @property
    def changes(self) -> int:
        """合并会改动多少东西；0 就是旧数据都已经在这里了。"""
        return sum(len(x) for x in (self.new_watches, self.new_channels, self.seen_new, self.seen_update,
                                    self.baseline_new, self.feed_new, self.status_new, self.kv_new, self.page_new,
                                    self.blocked_new))


def plan(src_cfg: dict, src: sqlite3.Connection | None, dst_cfg: dict, dst: sqlite3.Connection | None) -> Plan:
    """算出要怎么合并，不写任何东西。"""
    pl = Plan(fresh=not dst_cfg["watches"] and not dst_cfg["channels"])

    # —— 关注：同 ID，或者关键词相同，就是同一个 ——
    by_id = {w["id"]: w for w in dst_cfg["watches"]}
    by_key: dict[str, dict] = {}
    for w in dst_cfg["watches"]:
        by_key.setdefault(_watch_key(w), w)
    taken = set(by_id)
    for w in src_cfg["watches"]:
        hit = by_id.get(w["id"]) or by_key.get(_watch_key(w))
        if hit is not None:
            pl.watch_map[w["id"]] = hit["id"]
            pl.matched.append((w["name"], hit["name"]))
            continue
        nw = dict(w)
        if nw["id"] in taken:
            nw["id"] = new_id()
        taken.add(nw["id"])
        by_key.setdefault(_watch_key(nw), nw)
        pl.watch_map[w["id"]] = nw["id"]
        pl.new_watches.append(nw)
    names = {w["id"]: w["name"] for w in dst_cfg["watches"] + pl.new_watches}

    # —— 推送渠道：配置一模一样的只留一个 ——
    have = {_channel_key(c) for c in dst_cfg["channels"]}
    ids = {c["id"] for c in dst_cfg["channels"]}
    for c in src_cfg["channels"]:
        if _channel_key(c) in have:
            pl.same_channels.append(c.get("name") or c["type"])
            continue
        nc = dict(c)
        if nc["id"] in ids:
            nc["id"] = new_id()
        ids.add(nc["id"])
        have.add(_channel_key(nc))
        pl.new_channels.append(nc)

    # —— 设置：现在这边是空的就用旧的，否则保留现在的，只把不一样的列出来 ——
    for k in SETTINGS:
        if src_cfg[k] != dst_cfg[k]:
            pl.settings[k] = (src_cfg[k], dst_cfg[k])
    for k, v in src_cfg["web"].items():
        if k not in ("host", "port") and v != dst_cfg["web"].get(k):
            pl.settings[f"web.{k}"] = (v, dst_cfg["web"].get(k))

    if src is None:
        return pl
    tables = ("seen", "baseline", "feed", "status", "kv", "page", "blocked")
    s = {t: _rows(src, t) for t in tables}
    d = {t: (_rows(dst, t) if dst is not None else []) for t in tables}
    pl.src_counts = {t: len(v) for t, v in s.items()}
    pl.dst_counts = {t: len(v) for t, v in d.items()}

    def wid(old: str) -> str:
        return pl.watch_map.get(old, old)   # 配置里已经没有的关注：记录原样留着

    # 基线
    src_sig = {(b["watch_id"], b["source"]): b["sig"] for b in s["baseline"]}
    dst_base = {(b["watch_id"], b["source"]) for b in d["baseline"]}
    for b in s["baseline"]:
        key = (wid(b["watch_id"]), b["source"])
        if key not in dst_base:
            dst_base.add(key)
            pl.baseline_new.append({**b, "watch_id": key[0]})

    # 见过的商品：并集；两边都有的，最早见到取早的，最近见到取晚的，价格跟着最近的那次
    dst_seen = {(r["watch_id"], r["source"], r["item_id"]): r for r in d["seen"]}
    for r in s["seen"]:
        if _stale(r["source"], src_sig.get((r["watch_id"], r["source"]))):
            pl.seen_skipped[r["source"]] = pl.seen_skipped.get(r["source"], 0) + 1
            continue
        key = (wid(r["watch_id"]), r["source"], r["item_id"])
        cur = dst_seen.get(key)
        if cur is None:
            row = {**r, "watch_id": key[0]}
            dst_seen[key] = row
            pl.seen_new.append(row)
            continue
        merged = dict(cur)
        merged["first_seen"] = min(cur["first_seen"] or r["first_seen"], r["first_seen"] or cur["first_seen"])
        if (r["last_seen"] or 0) > (cur["last_seen"] or 0):
            merged["last_seen"], merged["price"] = r["last_seen"], r["price"]
        if merged != cur:
            dst_seen[key] = merged
            pl.seen_update.append(merged)

    # 推送记录：一模一样的不重复导入
    def fkey(r):
        return (r["source"], r["item_id"], r["kind"], r["price"], r["old_price"], r["found_at"])
    have_feed = {fkey(r) for r in d["feed"]}
    for r in s["feed"]:
        if fkey(r) in have_feed:
            pl.feed_dup += 1
            continue
        have_feed.add(fkey(r))
        w = wid(r["watch_id"])
        pl.feed_new.append({**r, "watch_id": w, "watch_name": names.get(w, r["watch_name"])})

    dst_status = {(r["watch_id"], r["source"]) for r in d["status"]}
    for r in s["status"]:
        key = (wid(r["watch_id"]), r["source"])
        if key not in dst_status:
            dst_status.add(key)
            pl.status_new.append({**r, "watch_id": key[0]})
    dst_kv = {r["key"] for r in d["kv"]}
    pl.kv_new = [r for r in s["kv"] if r["key"] not in dst_kv]
    dst_pages = {r["id"] for r in d["page"]}
    pl.page_new = [r for r in s["page"] if r["id"] not in dst_pages]
    dst_blocked = {(r["source"], r["item_id"]) for r in d["blocked"]}
    pl.blocked_new = [{**r, "watch_id": wid(r["watch_id"])} for r in s["blocked"]
                      if (r["source"], r["item_id"]) not in dst_blocked]
    return pl


def _insert(con: sqlite3.Connection, table: str, rows: list[dict], verb: str = "INSERT OR IGNORE") -> None:
    for r in rows:
        cols = list(r)
        con.execute(f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [r[c] for c in cols])


def write(pl: Plan, src_cfg: dict, data_dir: Path, store: Store | None = None,
          cfgstore: ConfigStore | None = None) -> Path:
    """按 plan 合并进 data_dir。先整份备份，数据库在一个事务里改完，出错就原样回滚。返回备份目录。

    网页里合并时传进正在用的 store、cfgstore，用同一个连接写；命令行合并时自己打开。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    # 中文别放进 strftime：Python 3.9 在英文 Windows 上会按系统编码处理格式串，编不了中文就报错
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = data_dir / f"迁移前备份-{stamp}"
    n = 1
    while backup.exists():
        n += 1
        backup = data_dir / f"迁移前备份-{stamp}-{n}"
    backup.mkdir()
    cfg_path, db_path = data_dir / "config.json", data_dir / "goodsmare.db"
    if cfg_path.exists():
        shutil.copy2(cfg_path, backup / "config.json")
    if db_path.exists():
        live = sqlite3.connect(str(db_path))
        copy = sqlite3.connect(str(backup / "goodsmare.db"))
        try:
            live.backup(copy)   # 连 WAL 里的一起，备份成一个完整的文件
        finally:
            copy.close()
            live.close()

    own = store is None
    if own:
        store = Store(db_path)   # 顺便建好新版才有的表
    con = store.db
    store._lock.acquire()
    try:
        con.execute("BEGIN IMMEDIATE")
        _insert(con, "baseline", pl.baseline_new)
        _insert(con, "seen", pl.seen_new)
        _insert(con, "seen", pl.seen_update, verb="INSERT OR REPLACE")
        _insert(con, "status", pl.status_new)
        _insert(con, "kv", pl.kv_new)
        _insert(con, "page", pl.page_new)
        _insert(con, "blocked", pl.blocked_new)
        if pl.feed_new:
            # 新旧记录混在一起按时间重排，上新页按 ID 倒序显示，顺序才对
            cols = ", ".join(FEED_COLS)
            con.execute(f"CREATE TEMP TABLE merged AS SELECT {cols}, 0 AS origin, id AS old FROM feed")
            for r in pl.feed_new:
                con.execute(f"INSERT INTO temp.merged VALUES ({', '.join('?' * (len(FEED_COLS) + 2))})",
                            [r[c] for c in FEED_COLS] + [1, r.get("id") or 0])
            con.execute("DELETE FROM feed")
            con.execute(f"INSERT INTO feed ({cols}) SELECT {cols} FROM temp.merged ORDER BY found_at, origin, old")
            con.execute("DROP TABLE temp.merged")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        store._lock.release()
        if own:
            con.close()

    def fn(cfg):
        cfg["watches"].extend(pl.new_watches)
        cfg["channels"].extend(pl.new_channels)
        if pl.fresh:
            for k in SETTINGS:
                cfg[k] = src_cfg[k]
            # 端口、监听地址留着现在的：旧版说不定还开着，占着那个端口
            cfg["web"].update({k: v for k, v in src_cfg["web"].items() if k not in ("host", "port")})
    (cfgstore or ConfigStore(cfg_path)).update(fn)
    return backup


def check(pl: Plan, data_dir: Path) -> list[str]:
    """合并完逐条核对：计划里要并进来的每一行都在。返回问题列表，空的就是都对。"""
    con = sqlite3.connect(str(data_dir / "goodsmare.db"))
    problems = []
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append("合并后的数据库完整性检查没通过")
        feed = set(con.execute("SELECT source, item_id, kind, price, old_price, found_at FROM feed"))
        lost = [r for r in pl.feed_new
                if (r["source"], r["item_id"], r["kind"], r["price"], r["old_price"], r["found_at"]) not in feed]
        if lost:
            problems.append(f"{len(lost)} 条推送记录没并进来")
        seen = set(con.execute("SELECT watch_id, source, item_id FROM seen"))
        lost = [r for r in pl.seen_new + pl.seen_update if (r["watch_id"], r["source"], r["item_id"]) not in seen]
        if lost:
            problems.append(f"{len(lost)} 件见过的商品没并进来")
    finally:
        con.close()
    cfg = ConfigStore(data_dir / "config.json").load()
    ids = {w["id"] for w in cfg["watches"]}
    if any(w["id"] not in ids for w in pl.new_watches):
        problems.append("有关注没加进配置")
    return problems


LABELS = {"interval": "扫描间隔（秒）", "request_gap": "同站请求间隔（秒）", "proxy": "代理", "jpy_to_cny": "汇率",
          "auto_rate": "自动汇率", "web.public_url": "手机访问地址", "web.push_page": "推送点开去推送页"}


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "开" if v else "关"
    return str(v) if v not in ("", None) else "空"


def report(pl: Plan, src_name: str, applied: bool, backup: Path | None = None, cli: bool = False) -> str:
    out = [f"从{src_name}{'合并完了' if applied else '合并的话会这样（这次只是看看，什么都没写）'}："]
    for old, new in pl.matched:
        out.append(f"  关注「{old}」和现在的「{new}」是同一个，记录并到「{new}」下面")
    for w in pl.new_watches:
        out.append(f"  新加关注「{w['name']}」（{'启用' if w['enabled'] else '停用'}）")
    for name in pl.same_channels:
        out.append(f"  推送渠道「{name}」两边一样，只留一个")
    for c in pl.new_channels:
        out.append(f"  新加推送渠道「{c.get('name') or c['type']}」（{'启用' if c.get('enabled', True) else '停用'}）")
    if pl.settings:
        shown = {k: v for k, v in pl.settings.items() if k != "web.token"}
        how = "现在这边还是空的，用旧的" if pl.fresh else "保留现在的"
        if shown:
            out.append(f"  设置不一样（{how}）：" + "；".join(
                f"{LABELS.get(k, k)} 旧 {_fmt(a)}，现在 {_fmt(b)}" for k, (a, b) in shown.items()))
        if "web.token" in pl.settings:
            out.append(f"  口令不一样（{how}）")
    out.append(f"  推送记录：并进来 {len(pl.feed_new)} 条" + (f"，{pl.feed_dup} 条已经有了" if pl.feed_dup else ""))
    out.append(f"  见过的商品：新增 {len(pl.seen_new)} 件，两边都有、更新了时间或价格的 {len(pl.seen_update)} 件")
    for src, n in pl.seen_skipped.items():
        name = SOURCES[src].name if src in SOURCES else src
        out.append(f"  {name} 的 {n} 件没并：旧版那时的解析有问题，价格不可信，并进来会报假降价（推送记录照样并了）")
    if pl.baseline_new or pl.status_new or pl.kv_new or pl.page_new or pl.blocked_new:
        out.append(f"  另外：基线 {len(pl.baseline_new)} 条、扫描状态 {len(pl.status_new)} 条、"
                   f"其他记录 {len(pl.kv_new)} 条、推送页 {len(pl.page_new)} 页、屏蔽的商品 {len(pl.blocked_new)} 件")
    if pl.src_counts:
        out.append(f"  旧数据（连 WAL 一起）：推送记录 {pl.src_counts['feed']} 条，见过的商品 {pl.src_counts['seen']} 件")
    if not applied and not pl.changes:
        out.append("  旧数据都已经在这里了，不用再合并。")
        return "\n".join(out)
    if applied and backup is not None:
        out.append(f"  合并前的数据备份在：{backup}")
        out.append("  旧版要是还开着，记得关掉它，不然两边会各推一遍。")
    elif cli:
        out.append("  确认没问题，先关掉正在运行的 goodsmare，再加上 --apply 真的合并。")
    return "\n".join(out)


def run(source, data_dir: Path, apply: bool = False, store: Store | None = None,
        cfgstore: ConfigStore | None = None) -> tuple[str, int]:
    """命令行和网页共用的入口，返回 (给人看的报告, 改动了/会改动多少东西)。

    store、cfgstore 不给就是命令行：自己打开，而且要求现在这份数据没人在用。
    """
    data_dir = Path(data_dir)
    src_cfg_path, src_db = locate(source)
    src_dir = (src_cfg_path or src_db).parent.resolve()
    if src_dir == data_dir.resolve():
        raise MigrateError("填的就是现在用的这个 data 文件夹，不用迁移")
    try:
        src_cfg = normalize(json.loads(src_cfg_path.read_text(encoding="utf-8"))) if src_cfg_path else normalize({})
    except ValueError as e:
        raise MigrateError(f"{src_cfg_path} 不是有效的 JSON：{e}") from e
    dst_cfg = (cfgstore or ConfigStore(data_dir / "config.json")).load()
    if apply and store is None and access.port_busy(dst_cfg["web"]["port"]):
        raise MigrateError(f"端口 {dst_cfg['web']['port']} 上有程序在运行，可能是 goodsmare 还开着。"
                           "先关掉它（关掉命令行窗口），或者直接在网页的「设置 → 从旧版迁移」里合并。")

    with tempfile.TemporaryDirectory(prefix="goodsmare-migrate-") as tmp:
        src = sqlite3.connect(str(snapshot(src_db, Path(tmp)))) if src_db else None
        dst_db = data_dir / "goodsmare.db"
        dst = None
        if dst_db.exists():
            # 演练时现在的程序可能还开着：拷一份快照来读，不碰正在用的文件
            (Path(tmp) / "dst").mkdir()
            dst = sqlite3.connect(str(snapshot(dst_db, Path(tmp) / "dst")))
        try:
            pl = plan(src_cfg, src, dst_cfg, dst)
        finally:
            for c in (src, dst):
                if c is not None:
                    c.close()
    # 报告里只写文件夹名和数据库名，完整路径太长
    folder = src_dir.parent if src_dir.name.lower() == "data" else src_dir
    name = f"「{folder.name}」" + (f"（{src_db.name}）" if src_db else "")
    if not apply:
        return report(pl, name, applied=False, cli=store is None), pl.changes
    backup = write(pl, src_cfg, data_dir, store, cfgstore)
    problems = check(pl, data_dir)
    if problems:
        raise MigrateError("合并后核对发现问题：" + "；".join(problems) + f"。合并前的数据在 {backup}，可以拷回去。")
    return report(pl, name, applied=True, backup=backup) + "\n  核对过了：要并进来的每一条都在。", pl.changes
