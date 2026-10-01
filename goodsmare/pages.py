"""推送页：每轮推送生成一页，推送里点开就是它。

页面在推送那一刻定格（价格、状态都是当时的），存在数据库里，由本程序的网页服务直接吐出 HTML，
不依赖管理网页的脚本和接口。链接里的随机 ID 就是钥匙，不用口令也能看。
"""

from __future__ import annotations

import html
import re
import time
from pathlib import Path

from . import APP_NAME
from .notify import Hit, cover, photo, src_name
from .store import PAGE_KEEP_DAYS as KEEP_DAYS

STATIC = Path(__file__).parent / "static"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16}$")

esc = html.escape

LOGO = ('<svg class="logo" viewBox="0 0 64 64" aria-hidden="true"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#f6849f"/><stop offset="1" stop-color="#d0346b"/></linearGradient></defs>'
        '<circle cx="32" cy="32" r="30" fill="url(#lg)"/><circle cx="32" cy="32" r="17" fill="none" stroke="#fff" stroke-width="4"/>'
        '<path d="M32 32 L51 18.6" stroke="#fff" stroke-width="4" stroke-linecap="round"/>'
        '<circle cx="32" cy="32" r="5" fill="#fff"/><circle cx="43.5" cy="45.5" r="3" fill="#fff"/></svg>')
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Ccircle cx='32' cy='32' r='30' "
           "fill='%23d0346b'/%3E%3Ccircle cx='32' cy='32' r='17' fill='none' stroke='white' stroke-width='4'/%3E%3Cpath d='M32 32 "
           "L51 18.6' stroke='white' stroke-width='4' stroke-linecap='round'/%3E%3Ccircle cx='32' cy='32' r='5' fill='white'/%3E%3C/svg%3E")


def snapshot(hits: list[Hit], rate: float) -> dict:
    """把一轮的命中存成推送页要用的数据。"""
    return {"at": int(time.time()), "rate": rate, "items": [{
        "source": h.item.source, "id": h.item.id, "title": h.item.title, "price": h.item.price,
        "old_price": h.old_price, "kind": h.kind, "url": h.item.url, "image": cover(h.item) or h.item.image,
        "photo": photo(h.item), "extra": h.item.extra, "created": h.item.created,
        "watch_id": h.watch_id, "watch_name": h.watch_name,
    } for h in hits]}


# —— 小工具 ——
def _site_colors() -> str:
    """网站代表色只在 index.html 里维护一份，这里照抄过来。"""
    if _site_colors.css is None:
        text = (STATIC / "index.html").read_text(encoding="utf-8")
        _site_colors.css = ";".join(dict.fromkeys(re.findall(r"--si?-[a-z_]+:\s*#[0-9a-fA-F]{3,8}", text)))
    return _site_colors.css


_site_colors.css = None


def _safe_url(url: str) -> str:
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else ""


def _when(ts: int) -> str:
    t = time.localtime(ts)
    return f"{t.tm_mon}月{t.tm_mday}日 {t.tm_hour:02d}:{t.tm_min:02d}"


def _yen(p) -> str:
    return "价格未知" if p is None else f"¥{p:,}"


def _cny(p, rate: float) -> str:
    return "" if p is None else f"≈ {p * rate:,.0f} 元"


def _watches(items: list[dict]) -> list[str]:
    names = []
    for it in items:
        if it["watch_name"] not in names:
            names.append(it["watch_name"])
    return names


def headline(items: list[dict]) -> str:
    drops = sum(1 for it in items if it["kind"] == "drop")
    parts = []
    if len(items) - drops:
        parts.append(f"上新 {len(items) - drops} 件")
    if drops:
        parts.append(f"降价 {drops} 件")
    return "，".join(parts)


# —— 页面 ——
CSS = """
:root { color-scheme: light;
  --bg: #f8f4f2; --panel: #fff; --panel-2: #faf6f4; --panel-3: #f2ebe8; --ink: #241f22; --ink-2: #5b5156; --muted: #71666c;
  --line: #ece4e1; --line-strong: #ddd2ce; --accent: #d0346b; --accent-ink: #c62f63; --accent-soft: #fde8ef;
  --btn-bg: linear-gradient(180deg, #d63a72, #c62f63); --btn-ink: #fff; --ok-solid: #15845a;
  --shadow: 0 1px 2px rgba(60, 30, 40, .05), 0 8px 24px rgba(60, 30, 40, .08);
  --font: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  --font-ja: "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic UI", "Meiryo UI", "Noto Sans CJK JP", var(--font);
  --mono: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, monospace; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark;
  --bg: #151116; --panel: #1f1a20; --panel-2: #262029; --panel-3: #2f2831; --ink: #f3ecef; --ink-2: #c9bec3; --muted: #a0959a;
  --line: #332c34; --line-strong: #463d48; --accent: #f06292; --accent-ink: #ff7aa8; --accent-soft: rgba(240, 98, 146, .16);
  --btn-bg: linear-gradient(180deg, #f57aa5, #ec5b8d); --btn-ink: #2a0d18; --shadow: 0 10px 30px rgba(0, 0, 0, .4); } }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.6 var(--font); -webkit-font-smoothing: antialiased; }
a { color: inherit; }
.i { width: 1.1em; height: 1.1em; flex: none; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.top { position: sticky; top: 0; z-index: 5; display: flex; align-items: center; gap: 10px; padding: 10px 16px; padding-top: max(10px, env(safe-area-inset-top));
  background: color-mix(in srgb, var(--bg) 82%, transparent); backdrop-filter: saturate(1.5) blur(14px); -webkit-backdrop-filter: saturate(1.5) blur(14px);
  border-bottom: 1px solid var(--line); }
.brand { display: flex; align-items: center; gap: 8px; font-weight: 750; font-size: 16px; text-decoration: none; }
.logo { width: 28px; height: 28px; }
.top .manage { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 13px; border-radius: 999px;
  border: 1px solid var(--line-strong); background: var(--panel); font-size: 13.5px; font-weight: 600; text-decoration: none; color: var(--ink-2); }
main { max-width: 760px; margin: 0 auto; padding: 18px 16px 40px; padding-bottom: max(40px, env(safe-area-inset-bottom)); }
.hero h1 { margin: 2px 0 2px; font-size: 21px; line-height: 1.35; font-weight: 750; letter-spacing: .2px; overflow-wrap: anywhere; }
.hero p { margin: 0; color: var(--muted); font-size: 13.5px; }
.jump { display: flex; gap: 8px; margin: 14px -16px 0; padding: 2px 16px 4px; overflow-x: auto; scrollbar-width: none; }
.jump::-webkit-scrollbar { display: none; }
.jump a { position: relative; flex: none; width: 58px; height: 58px; border-radius: 12px; overflow: hidden; background: var(--panel-3); border: 1px solid var(--line); }
.jump img { width: 100%; height: 100%; object-fit: cover; display: block; }
.jump b { position: absolute; left: 3px; bottom: 3px; padding: 0 5px; border-radius: 6px; background: rgba(20, 10, 15, .62); color: #fff; font-size: 11px; line-height: 1.5; }
.item { margin-top: 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 20px; overflow: hidden; box-shadow: var(--shadow); scroll-margin-top: 64px; }
.item:target { border-color: color-mix(in srgb, var(--accent) 55%, var(--line)); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent), var(--shadow); }
.pic { position: relative; display: block; aspect-ratio: 1; background: var(--panel-3); }
.pic img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; display: block; }
.pic .none { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); font-size: 13px; }
.badge { position: absolute; z-index: 1; top: 12px; left: 12px; padding: 3px 11px; border-radius: 999px; background: var(--c, #71666c); color: var(--ci, #fff);
  font-size: 12.5px; font-weight: 650; box-shadow: 0 2px 8px rgba(0, 0, 0, .2); }
.kind { position: absolute; z-index: 1; top: 12px; right: 12px; padding: 3px 11px; border-radius: 999px; background: var(--accent); color: #fff;
  font-size: 12.5px; font-weight: 650; box-shadow: 0 2px 8px rgba(0, 0, 0, .2); }
.kind.drop { background: var(--ok-solid); }
.body { padding: 16px 18px 18px; }
.body h2 { margin: 0; font: 600 16.5px/1.55 var(--font-ja); overflow-wrap: anywhere; }
.price { display: flex; align-items: baseline; flex-wrap: wrap; gap: 2px 10px; margin-top: 8px; }
.price b { font-size: 28px; font-weight: 800; letter-spacing: -.3px; color: var(--accent-ink); font-variant-numeric: tabular-nums; }
.price b i { font-style: normal; font-size: .62em; margin-right: 1px; }
.price .cny { color: var(--ink-2); font-size: 14.5px; font-weight: 600; }
.price .unknown { color: var(--muted); font-size: 16px; font-weight: 600; }
.was { color: var(--muted); font-size: 13.5px; margin-top: 2px; }
.facts { margin: 14px 0 0; padding: 4px 14px; border-radius: 14px; background: var(--panel-2); border: 1px solid var(--line); }
.facts div { display: grid; grid-template-columns: 4.2em 1fr; gap: 10px; padding: 8px 0; font-size: 14px; }
.facts div + div { border-top: 1px solid var(--line); }
.facts dt { color: var(--muted); }
.facts dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.facts dd a { margin-left: 8px; color: var(--accent-ink); font-weight: 600; text-decoration: none; white-space: nowrap; }
.facts .ago { color: var(--muted); }
.acts { display: grid; grid-template-columns: 1fr auto; gap: 10px; margin-top: 16px; }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; height: 46px; padding: 0 18px; border-radius: 13px; border: 1px solid var(--line-strong);
  background: var(--panel); color: var(--ink); font: 650 15px var(--font); text-decoration: none; cursor: pointer; -webkit-tap-highlight-color: transparent; }
.btn:active { transform: translateY(1px); }
.btn.primary { background: var(--btn-bg); color: var(--btn-ink); border-color: transparent; box-shadow: inset 0 1px 0 rgba(255, 255, 255, .22), 0 8px 18px -8px rgba(208, 52, 107, .55); }
.url { margin-top: 10px; font: 12.5px/1.5 var(--mono); color: var(--muted); overflow-wrap: anywhere; user-select: all; -webkit-user-select: all; }
footer { margin-top: 26px; text-align: center; color: var(--muted); font-size: 13px; line-height: 1.8; }
footer nav { display: flex; justify-content: center; gap: 10px; margin-bottom: 10px; }
footer nav a { display: inline-flex; align-items: center; gap: 6px; height: 38px; padding: 0 15px; border-radius: 12px; border: 1px solid var(--line-strong);
  background: var(--panel); color: var(--ink-2); font-weight: 600; text-decoration: none; }
.msg { margin: 12vh auto 0; max-width: 420px; text-align: center; }
.msg h1 { margin: 14px 0 6px; font-size: 21px; }
.msg p { margin: 0 0 8px; color: var(--ink-2); }
.msg .logo { width: 56px; height: 56px; }
.msg form { display: flex; gap: 8px; margin: 18px 0 12px; }
.msg input { flex: 1; min-width: 0; height: 46px; padding: 0 14px; border-radius: 13px; border: 1px solid var(--line-strong); background: var(--panel); color: var(--ink); font: 15px var(--font); }
.msg small { display: block; color: var(--muted); font-size: 12.5px; line-height: 1.7; }
#toast { position: fixed; left: 50%; bottom: calc(24px + env(safe-area-inset-bottom)); transform: translate(-50%, 12px); max-width: calc(100vw - 32px);
  padding: 10px 16px; border-radius: 12px; background: var(--ink); color: var(--bg); font-size: 14px; font-weight: 600; opacity: 0; pointer-events: none; transition: opacity .2s, transform .25s; }
#toast.show { opacity: 1; transform: translate(-50%, 0); }
@media (min-width: 700px) {
  .top { padding-left: max(16px, calc((100% - 760px) / 2 + 16px)); padding-right: max(16px, calc((100% - 760px) / 2 + 16px)); }
  .item { display: grid; grid-template-columns: 300px 1fr; align-items: start; }
  .acts { grid-template-columns: auto auto; justify-content: start; }
}
"""

ICON_GEAR = ('<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h8M16.5 7H20M4 17h3M11.5 17H20"/>'
             '<circle cx="14.2" cy="7" r="2.2"/><circle cx="9.3" cy="17" r="2.2"/></svg>')
ICON_OUT = ('<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9"/>'
            '<path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/></svg>')
ICON_COPY = ('<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/>'
             '<path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/></svg>')
ICON_PLUS = '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>'

SCRIPT = """
function toast(t) { var el = document.getElementById("toast"); el.textContent = t; el.className = "show";
  clearTimeout(toast.t); toast.t = setTimeout(function () { el.className = ""; }, 2200); }
function copyOld(text) {
  var ta = document.createElement("textarea"); ta.value = text; ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0"; document.body.appendChild(ta);
  ta.select(); ta.setSelectionRange(0, text.length);
  var ok = false; try { ok = document.execCommand("copy"); } catch (e) {}
  ta.remove(); return ok;
}
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-copy]"); if (!b) return;
  var text = b.getAttribute("data-copy");
  var done = function (ok) { toast(ok ? "链接已复制" : "复制不了，长按下面的链接手动复制"); };
  // 局域网的 http 页面不是安全上下文，没有 navigator.clipboard，用老办法
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(copyOld(text)); });
  else done(copyOld(text));
});
(function () {
  var now = Date.now() / 1000;
  document.querySelectorAll("[data-ts]").forEach(function (el) {
    var d = Math.max(0, now - Number(el.getAttribute("data-ts"))), s;
    if (d < 60) s = "刚刚"; else if (d < 3600) s = Math.floor(d / 60) + " 分钟前";
    else if (d < 86400) s = Math.floor(d / 3600) + " 小时前"; else s = Math.floor(d / 86400) + " 天前";
    el.textContent = "（" + s + "）";
  });
})();
"""


def _doc(title: str, body: str, script: bool = False) -> str:
    return (
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="color-scheme" content="light dark">\n'
        '<meta name="referrer" content="no-referrer">\n<meta name="robots" content="noindex, nofollow">\n'
        f'<title>{esc(title)}</title>\n<link rel="icon" href="{FAVICON}">\n'
        f'<style>:root{{{_site_colors()}}}{CSS}</style>\n</head>\n<body>\n{body}\n'
        + (f'<div id="toast" role="status" aria-live="polite"></div>\n<script>{SCRIPT}</script>\n' if script else "")
        + "</body>\n</html>\n")


def _item(n: int, it: dict, rate: float, at: int) -> str:
    url = _safe_url(it["url"])
    big, small = _safe_url(it.get("photo", "")), _safe_url(it.get("image", ""))
    src = big or small
    style = f"--c:var(--s-{esc(it['source'])},#71666c);--ci:var(--si-{esc(it['source'])},#fff)"
    if src:
        fallback = (f' data-small="{esc(small)}" onerror="this.onerror=null;if(this.dataset.small)this.src=this.dataset.small"'
                    if small and small != src else "")
        lazy = "" if n <= 2 else ' loading="lazy"'   # 头两张马上加载，后面的滚到了再加载
        pic = f'<img src="{esc(src)}" alt=""{fallback} decoding="async"{lazy}>'
    else:
        pic = '<span class="none">没有图片</span>'
    drop = it["kind"] == "drop"
    old = it.get("old_price")
    kind = ""
    was = ""
    if drop and old and it["price"] is not None:
        pct = round((1 - it["price"] / old) * 100)
        kind = f'<span class="kind drop">降价{f" −{pct}%" if pct > 0 else ""}</span>'
        was = f'<div class="was">原价 <s>{_yen(old)}</s>，便宜了 {_yen(old - it["price"])}</div>'
    elif not drop:
        kind = '<span class="kind">上新</span>'
    price = ('<span class="unknown">价格未知</span>' if it["price"] is None
             else f'<b><i>¥</i>{it["price"]:,}</b><span class="cny">{_cny(it["price"], rate)}</span>')

    facts = [("网站", esc(src_name(it["source"])))]
    if it.get("extra"):
        facts.append(("说明", esc(it["extra"])))
    if it.get("created"):
        facts.append(("上架", f'{_when(it["created"])}<span class="ago" data-ts="{int(it["created"])}"></span>'))
    facts.append(("发现", f'{_when(at)}<span class="ago" data-ts="{at}"></span>'))
    watch = esc(it["watch_name"])
    if it.get("watch_id") and it["watch_id"] != "test":
        watch += f'<a href="/#watch={esc(it["watch_id"])}">改条件</a>'
    facts.append(("关注", watch))
    facts.append(("编号", esc(it["id"])))
    rows = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)

    link = f'href="{esc(url)}" rel="noreferrer"' if url else ""
    acts = (f'<div class="acts"><a class="btn primary" {link}>{ICON_OUT}打开原商品页</a>'
            f'<button class="btn" type="button" data-copy="{esc(url)}">{ICON_COPY}复制链接</button></div>'
            f'<div class="url">{esc(url)}</div>') if url else ""
    return (f'<article class="item" id="i{n}" style="{style}">'
            f'<a class="pic" {link} aria-label="打开原商品页">{pic}<span class="badge">{esc(src_name(it["source"]))}</span>{kind}</a>'
            f'<div class="body"><h2 lang="ja">{esc(it["title"])}</h2><div class="price">{price}</div>{was}'
            f'<dl class="facts">{rows}</dl>{acts}</div></article>')


def render(page: dict) -> str:
    items, rate, at = page["items"], page.get("rate") or 0, int(page.get("at") or 0)
    names = _watches(items)
    label = "、".join(names[:3]) + ("等" if len(names) > 3 else "")
    title = f"{label} {headline(items)}"
    jump = ""
    if len(items) > 1:
        thumbs = []
        for n, it in enumerate(items, 1):
            img = _safe_url(it.get("image", ""))
            thumbs.append(f'<a href="#i{n}" aria-label="第 {n} 件">'
                          + (f'<img src="{esc(img)}" alt="" loading="lazy">' if img else "") + f"<b>{n}</b></a>")
        jump = f'<nav class="jump" aria-label="这次的商品">{"".join(thumbs)}</nav>'
    body = (
        f'<header class="top"><a class="brand" href="/">{LOGO}<span>{APP_NAME}</span></a>'
        f'<a class="manage" href="/">{ICON_GEAR}管理关注</a></header>\n<main>\n'
        f'<section class="hero"><h1>{esc(title)}</h1><p>{_when(at)} 推送 · 共 {len(items)} 件</p></section>\n'
        f"{jump}\n" + "\n".join(_item(n, it, rate, at) for n, it in enumerate(items, 1)) +
        f'\n<footer><nav><a href="/#new">{ICON_PLUS}新建关注</a><a href="/">{ICON_GEAR}管理关注</a></nav>'
        f"价格和状态是推送那一刻的，以原网页为准。<br>这个页面保留 {KEEP_DAYS} 天。</footer>\n</main>")
    return _doc(f"{title} · {APP_NAME}", body, script=True)


def render_missing() -> str:
    body = (f'<div class="msg">{LOGO}<h1>这个推送页已经不在了</h1>'
            f"<p>推送页只保留 {KEEP_DAYS} 天，或者数据被清掉了。</p>"
            f'<footer><nav><a href="/">{ICON_GEAR}打开 {APP_NAME}</a></nav></footer></div>')
    return _doc(f"页面不存在 · {APP_NAME}", body)


def render_login(wrong: bool = False) -> str:
    tip = "<p>口令不对，再试一次。</p>" if wrong else ""
    body = (f'<div class="msg" style="padding:0 16px">{LOGO}<h1>要先输入口令</h1>{tip}'
            f"<p>在电脑上打开 {APP_NAME}，进「设置 → 手机访问」，用手机扫那里的二维码，口令会自动带上。</p>"
            '<form method="get" action="/" onsubmit="this.action=\'/\'+location.hash">'
            '<input name="token" type="password" placeholder="或者直接输入口令" autocomplete="current-password" aria-label="口令" required>'
            '<button class="btn primary" type="submit">进入</button></form>'
            "<small>口令也写在电脑上 data/config.json 的 web.token 里。<br>输对一次之后，这个浏览器会记住一年。</small></div>")
    return _doc(f"需要口令 · {APP_NAME}", body)
