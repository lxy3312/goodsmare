import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from goodsmare import access, migrate
from goodsmare.config import ConfigStore
from goodsmare.notify import Hit
from goodsmare.sources import Item
from goodsmare.store import Store

DAY = 86400
T0 = 1790000000


def item(i, price=1000, source="mercari"):
    return Item(source=source, id=f"m{i}", title=f"フィッシュマンズ {i}", price=price,
                url=f"https://jp.mercari.com/item/m{i}")


def add_feed(store, it, watch_id, name, at, kind="new", old=None):
    rid = store.add_feed(Hit(it, watch_id, name, kind, old))
    store.db.execute("UPDATE feed SET found_at=? WHERE id=?", (at, rid))
    store.db.commit()


def seen(store, watch_id, source, item_id, price, first, last):
    store.db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?,?,?,?)", (watch_id, source, item_id, price, first, last))
    store.db.commit()


def rows(path, sql):
    con = sqlite3.connect(str(path))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_app = root / "ritao"
        self.old = self.old_app / "data"
        self.new = root / "goodsmare" / "data"
        self.old.mkdir(parents=True)
        self.new.mkdir(parents=True)
        self.patch = mock.patch.object(access, "port_busy", return_value=False)
        self.patch.start()

        # 旧版：数据库叫 ritao.db；一个和新版重名的关注、一个新版没有的关注、一个企业微信
        ConfigStore(self.old / "config.json").save({
            "interval": 30, "proxy": "http://127.0.0.1:7890",
            "watches": [{"id": "old1", "name": "fishmans", "keyword": "fishmans", "price_drop": True},
                        {"id": "old2", "name": "空中キャンプ", "keyword": "空中キャンプ", "enabled": False}],
            "channels": [{"id": "c1", "type": "wecom", "webhook": "https://qyapi.weixin.qq.com/k"}]})
        self.old_store = Store(self.old / "ritao.db")
        # 不让 SQLite 自动把 WAL 写回主文件：最近的改动都只在 ritao.db-wal 里，就像程序还开着或者被直接关掉
        self.old_store.db.execute("PRAGMA wal_autocheckpoint=0")
        s = self.old_store
        s.set_baseline("old1", "mercari", "fishmans|0|0|")
        s.set_baseline("old1", "surugaya", "fishmans|0|0|")         # 旧解析记的骏河屋，没有 |rev2
        seen(s, "old1", "mercari", "m1", 1000, T0, T0 + DAY)          # 两边都见过
        seen(s, "old1", "mercari", "m2", 1500, T0, T0 + DAY)          # 只有旧版见过
        seen(s, "old1", "surugaya", "999", 2750, T0, T0 + DAY)        # 价格不可信，不并
        seen(s, "old2", "mercari", "m7", 800, T0, T0 + DAY)
        add_feed(s, item(1), "old1", "fishmans", T0 + 100)
        add_feed(s, item(2, 1500), "old1", "fishmans", T0 + 200)
        add_feed(s, item(3, 900), "old1", "fishmans", T0 + 300, "drop", 1200)
        add_feed(s, item(7, 800), "old2", "空中キャンプ", T0 + 400)

        # 新版：关键词大小写、空格不同，但是同一个关注；同一个企业微信；设置改过
        self.new_cfg = ConfigStore(self.new / "config.json")
        self.new_cfg.save({"interval": 180, "watches": [
            {"id": "new1", "name": "fishmans", "keyword": " Fishmans ", "price_drop": True},
            {"id": "new2", "name": "ミドリ", "keyword": "ミドリ cd"}],
            "channels": [{"id": "c9", "type": "wecom", "name": "群", "webhook": "https://qyapi.weixin.qq.com/k"}]})
        st = Store(self.new / "goodsmare.db")
        st.set_baseline("new1", "mercari", "fishmans|0|0|")
        st.set_baseline("new1", "surugaya", "fishmans|0|0||rev2")
        seen(st, "new1", "mercari", "m1", 950, T0 + 2 * DAY, T0 + 3 * DAY)
        add_feed(st, item(1, 950), "new1", "fishmans", T0 + 2 * DAY, "drop", 1000)
        add_feed(st, item(5), "new2", "ミドリ", T0 + 3 * DAY)
        st.db.close()

    def tearDown(self):
        self.patch.stop()
        self.old_store.db.close()
        self.tmp.cleanup()

    def migrate(self, apply=True, source=None):
        return migrate.run(source or self.old_app, self.new, apply=apply)[0]

    def test_recent_changes_live_in_the_wal(self):
        # 只拷 ritao.db 的话，连表都还没写回主文件——这就是要连 WAL 一起读的原因
        only = Path(self.tmp.name) / "only-main"
        only.mkdir()
        shutil.copy2(self.old / "ritao.db", only / "ritao.db")
        self.assertNotIn(("feed",), rows(only / "ritao.db", "SELECT name FROM sqlite_master"))
        self.assertTrue((self.old / "ritao.db-wal").exists())
        self.migrate()
        self.assertEqual(len(rows(self.new / "goodsmare.db", "SELECT * FROM feed")), 6)

    def test_dry_run_writes_nothing(self):
        before = rows(self.new / "goodsmare.db", "SELECT * FROM feed")
        cfg = (self.new / "config.json").read_bytes()
        text = self.migrate(apply=False)
        self.assertIn("只是看看", text)
        self.assertIn("「fishmans」和现在的「fishmans」是同一个", text)
        self.assertIn("并进来 4 条", text)
        self.assertEqual(rows(self.new / "goodsmare.db", "SELECT * FROM feed"), before)
        self.assertEqual((self.new / "config.json").read_bytes(), cfg)
        self.assertEqual([p.name for p in self.new.iterdir() if p.name.startswith("迁移前备份")], [])

    def test_merge_keeps_both_sides(self):
        text = self.migrate()
        self.assertIn("核对过了", text)
        db = self.new / "goodsmare.db"
        feed = rows(db, "SELECT item_id, watch_id, watch_name, kind, found_at FROM feed ORDER BY id")
        # 新旧记录按时间排好；同名关注的记录并到现在这个下面
        self.assertEqual([f[4] for f in feed], sorted(f[4] for f in feed))
        self.assertEqual(feed[0][:3], ("m1", "new1", "fishmans"))
        self.assertEqual(feed[-1][:3], ("m5", "new2", "ミドリ"))
        self.assertEqual(len(feed), 6)
        self.assertIn(("m7", "old2", "空中キャンプ", "new", T0 + 400), feed)
        # 见过的商品：并集；两边都有的，最早见到取早的，价格跟最近那次
        s = dict(((w, src, i), (p, f, l)) for w, src, i, p, f, l in rows(db, "SELECT * FROM seen"))
        self.assertEqual(s[("new1", "mercari", "m1")], (950, T0, T0 + 3 * DAY))
        self.assertEqual(s[("new1", "mercari", "m2")], (1500, T0, T0 + DAY))
        self.assertNotIn(("new1", "surugaya", "999"), s, "旧解析的骏河屋价格不并")
        self.assertIn("骏河屋 的 1 件没并", text)
        # 基线：现在这边有的不动
        base = dict(((w, src), sig) for w, src, sig, _ in rows(db, "SELECT * FROM baseline"))
        self.assertEqual(base[("new1", "surugaya")], "fishmans|0|0||rev2")
        # 配置：不多出重复的关注和渠道；新版的设置不变；旧版独有的关注加进来，保持停用
        cfg = self.new_cfg.load()
        self.assertEqual([w["id"] for w in cfg["watches"]], ["new1", "new2", "old2"])
        self.assertFalse(cfg["watches"][2]["enabled"])
        self.assertEqual(len(cfg["channels"]), 1)
        self.assertEqual(cfg["interval"], 180)
        self.assertEqual(cfg["proxy"], "")
        # 合并前的备份
        backup = next(p for p in self.new.iterdir() if p.name.startswith("迁移前备份"))
        self.assertEqual(len(rows(backup / "goodsmare.db", "SELECT * FROM feed")), 2)
        self.assertIn('"interval": 180', (backup / "config.json").read_text(encoding="utf-8"))

    def test_twice_is_the_same_as_once(self):
        self.migrate()
        once = rows(self.new / "goodsmare.db", "SELECT * FROM feed ORDER BY id")
        text, changes = migrate.run(self.old_app, self.new)
        self.assertEqual(changes, 0)
        self.assertIn("并进来 0 条，4 条已经有了", text)
        self.assertIn("不用再合并", text)
        text = self.migrate()
        self.assertEqual(rows(self.new / "goodsmare.db", "SELECT * FROM feed ORDER BY id"), once)
        self.assertEqual(len(self.new_cfg.load()["watches"]), 3)

    def test_fresh_install_takes_everything(self):
        fresh = Path(self.tmp.name) / "fresh" / "data"
        text, changes = migrate.run(self.old, fresh, apply=True)   # 给 data 文件夹也行
        self.assertGreater(changes, 0)
        self.assertIn("核对过了", text)
        cfg = ConfigStore(fresh / "config.json").load()
        self.assertEqual([w["id"] for w in cfg["watches"]], ["old1", "old2"])
        self.assertEqual(cfg["interval"], 30, "新装的还没配过，沿用旧版的设置")
        self.assertEqual(cfg["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(len(rows(fresh / "goodsmare.db", "SELECT * FROM feed")), 4)
        self.assertEqual(rows(fresh / "goodsmare.db", "SELECT COUNT(*) FROM page"), [(0,)], "新版的表也建好了")

    def test_copies_files_when_read_only_open_fails(self):
        missing = (Path(self.tmp.name) / "no-such-dir" / "x.db").as_uri()   # 只读打开这个一定失败
        with mock.patch.object(Path, "as_uri", return_value=missing):
            self.migrate()
        self.assertEqual(len(rows(self.new / "goodsmare.db", "SELECT * FROM feed")), 6)

    def test_old_folder_is_untouched(self):
        before = {p.name: p.read_bytes() for p in self.old.iterdir() if not p.name.endswith("-shm")}
        self.migrate()
        after = {p.name: p.read_bytes() for p in self.old.iterdir() if not p.name.endswith("-shm")}
        self.assertEqual(before, after)

    def test_refuses_when_goodsmare_is_running(self):
        with mock.patch.object(access, "port_busy", return_value=True):
            with self.assertRaises(migrate.MigrateError) as cm:
                self.migrate()
        self.assertIn("还开着", str(cm.exception))
        self.migrate(apply=False)   # 只看看可以

    def test_bad_folders(self):
        with self.assertRaises(migrate.MigrateError):
            migrate.run(Path(self.tmp.name) / "nothing", self.new)
        with self.assertRaises(migrate.MigrateError):
            migrate.run(self.new, self.new)


if __name__ == "__main__":
    unittest.main()
