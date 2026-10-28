"""推送渠道。每种渠道一个函数：(渠道配置, 命中列表, 上下文) → 抛异常表示失败。

几秒内一起查到的新货合成一次推送；Bark / ntfy / Telegram 这类手机通知逐条发，超出上限的合成一条汇总。
内容尽量短：一件一行「价格 标题」，点开直接去买（设置里选了任意门，就是任意门的商品页），后面跟网站名。

手机能连上本程序时（见 access.py），每次推送还会生成一个推送页（pages.py），消息开头放它的链接。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import smtplib
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from . import APP_NAME, message_layout, net
from .sources import SOURCES, Item, Query

PER_ITEM_LIMIT = 6   # 逐条推送的渠道，一轮最多单独推几条


@dataclass
class Hit:
    item: Item
    watch_id: str
    watch_name: str
    kind: str = "new"              # new 上新 / drop 降价
    old_price: int | None = None
    quiet: bool = False            # 程序停过一阵后补查到的：记进上新，不一定推送


@dataclass
class Ctx:
    rate: float                    # 1 日元 = ? 人民币
    web_url: str = ""              # 手机能打开的本程序地址，没有就空
    page_url: str = ""             # 这一次的推送页，没有就空
    buy: str = ""                  # 点开商品去哪：空是原网页，doorzo 是任意门
    message: dict | None = None
    channel_kind: str = ""
    keywords: dict = field(default_factory=dict)
    queries: dict = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time()))
    item_offset: int = 0            # 用户指定每条件数后，仍指向整批推送页的正确商品


# 任意门（日淘任意门 / Doorzo）网页版的商品页是 /mall/<站>/detail/<商品号>，2026 年 10 月照它网页的路由写的。
# 它没有 Mandarake、animate、闲鱼，这几个站还是打开原网页。
DOORZO = {"mercari": "mercari", "yahoo_auction": "yahoo", "yahoo_flea": "paypay", "rakuma": "rakuma",
          "surugaya": "surugaya"}
BUY_TARGETS = {"": "原网页", "doorzo": "任意门"}


def buy_url(it: Item, buy: str) -> str:
    """去哪买：选了任意门、任意门又有这个网站时，打开任意门的商品页；别的照旧开原网页。"""
    if buy == "doorzo" and it.source in DOORZO and it.id and "/shops/" not in it.url:
        return f"https://www.doorzo.cn/mall/{DOORZO[it.source]}/detail/{urllib.parse.quote(it.id, safe='')}"
    return it.url


def link(h: Hit, ctx: Ctx, i: int) -> str:
    """推送里点开去哪：能去任意门就直接去买；不然有推送页去推送页上那一件，没有就去商品页。"""
    i += ctx.item_offset
    target = ctx.message.get("click_target", "inherit") if ctx.message else "inherit"
    if target == "doorzo":
        return buy_url(h.item, "doorzo")
    if target == "original":
        return h.item.url
    if target == "page":
        return f"{ctx.page_url}#i{i + 1}" if ctx.page_url else h.item.url
    go = buy_url(h.item, ctx.buy)
    if go != h.item.url:
        return go
    return f"{ctx.page_url}#i{i + 1}" if ctx.page_url else h.item.url


def _message_data(h: Hit, ctx: Ctx, i: int = 0) -> dict:
    it = h.item
    original, doorzo = it.url, buy_url(it, "doorzo")
    keyword = str(ctx.keywords.get(h.watch_id) or "")

    def date(ts):
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
        except (ValueError, OverflowError, OSError):
            return ""

    drop = ""
    if h.kind == "drop" and h.old_price is not None and it.price is not None:
        percent = round((1 - it.price / h.old_price) * 100) if h.old_price > 0 else 0
        drop = f"{money(h.old_price, it.source)} → {money(it.price, it.source)}，便宜 {money(h.old_price - it.price, it.source)}（{percent}%）"
    return {"title": it.title, "price": money(it.price, it.source),
            "cny": cny(it.price, ctx.rate) if not is_cny(it.source) else "", "drop": drop,
            "source": src_name(it.source), "watch": short_title(h.watch_name, 100), "keyword": short_title(keyword, 150),
            "extra": short_title(it.extra, 300), "published": date(it.created), "found": date(ctx.created_at),
            "item_id": it.id, "status": "已售" if it.sold else "降价" if h.kind == "drop" else "上新",
            "image": cover(it) if ctx.message and ctx.message["image_size"] == "small" else photo(it),
            "full_image": photo(it),
            "original_url": original, "buy_url": doorzo if doorzo != original else "", "link": link(h, ctx, i),
            "page_url": f"{ctx.page_url}#i{ctx.item_offset + i + 1}" if ctx.page_url else "",
            "manage_url": f"{ctx.web_url}/#watch={urllib.parse.quote(h.watch_id, safe='')}" if ctx.web_url else "",
            "search_url": SOURCES[it.source].search_url(ctx.queries.get(h.watch_id) or Query(keyword)) if keyword and it.source in SOURCES else ""}


def _message_body(h: Hit, ctx: Ctx, i: int, format: str) -> str:
    return message_layout.render(_message_data(h, ctx, i), ctx.message, ctx.channel_kind, format)


def _message_photo(it: Item, ctx: Ctx) -> str:
    if not message_layout.enabled(ctx.message, "image"):
        return ""
    return cover(it) if ctx.message and ctx.message["image_size"] == "small" else photo(it)


# 每种渠道需要填的字段，网页的表单按这个生成。(字段, 标签, 说明, 是否必填[, "bool" 表示开关[, 开关默认值，不写是开]])
CHANNEL_TYPES = {
    "serverchan": ("Server酱（微信）", [
        ("sendkey", "SendKey", "sct.ftqq.com 登录后获取；Server酱³ 的 sctp 开头的 key 也行", True)]),
    "pushplus": ("PushPlus（微信）", [
        ("token", "Token", "www.pushplus.plus 登录后获取", True),
        ("topic", "群组编码", "一对多推送时填，可空", False)]),
    "wxpusher": ("WxPusher（推荐用 SPT）", [
        ("token", "推送令牌", "自己用：扫码获取 SPT_ 开头的令牌即可。也支持 AT_ 应用令牌；多个 SPT 用逗号隔开", True),
        ("uids", "接收 UID", "SPT 不用填；AT 令牌要填 UID 或主题 ID，多个用逗号隔开", False),
        ("topic_ids", "主题 ID", "SPT 不用填；应用群发时填数字 ID，多个用逗号隔开", False)]),
    "xtuis": ("虾推啥（微信）", [
        ("token", "Token", "微信关注虾推啥公众号后获取。免费额度每天 300 条、每分钟 30 条", True)]),
    "iyuu": ("爱语飞飞 IYUU（微信）", [
        ("token", "IYUU 令牌", "iyuu.cn 微信扫码获取，IYUU 开头；按 IP 每小时限 20 条", True)]),
    "pushme": ("PushMe（安卓）", [
        ("push_key", "Push Key", "安装 PushMe，打开设置里的 Push Key 并点「获取」；需允许后台运行和通知", True),
        ("server", "服务器", "默认 https://push.i-i.me；自建时填完整的 http(s) 地址", False)]),
    "wecom": ("企业微信群机器人", [
        ("webhook", "Webhook 地址", "群设置 → 群机器人 → 添加，复制 Webhook 地址", True),
        ("cards", "用图文卡片（带小图）", "关着时一件一行文字，最短；打开后每件一张带小图的卡片，一条消息最多 8 件",
         False, "bool", False)]),
    "bark": ("Bark（iPhone）", [
        ("key", "设备 Key", "Bark App 首页的链接里 api.day.app/ 后面那段", True),
        ("server", "服务器", "自建时填，默认 https://api.day.app", False)]),
    "dingtalk": ("钉钉群机器人", [
        ("webhook", "Webhook 地址", "安全设置选“自定义关键词”就填 goodsmare；选“加签”就把密钥填下面", True),
        ("secret", "加签密钥", "SEC 开头，可空", False)]),
    "feishu": ("飞书群机器人", [
        ("webhook", "Webhook 地址", "群设置 → 群机器人 → 自定义机器人", True),
        ("secret", "签名密钥", "开了签名校验才填", False)]),
    "telegram": ("Telegram", [
        ("token", "Bot Token", "找 @BotFather 创建", True),
        ("chat_id", "Chat ID", "给 bot 发条消息后在 getUpdates 里看", True),
        ("api", "API 地址", "默认 https://api.telegram.org，用反代时改", False)]),
    "discord": ("Discord", [
        ("webhook", "Webhook 地址", "频道设置 → 整合 → Webhook → 新建并复制地址；普通文字频道即可", True)]),
    "pushover": ("Pushover（客户端需买断）", [
        ("token", "应用 API Token", "在 pushover.net 注册应用后获取；客户端试用结束后需按平台买断", True),
        ("user_key", "User / Group Key", "Pushover 首页的用户 Key，或接收群组的 Group Key", True),
        ("device", "设备名", "不填就发到这个用户的所有设备；多个用英文逗号隔开", False),
        ("sound", "提示音", "不填用手机默认提示音；例如 pushover、cashregister、none", False)]),
    "ntfy": ("ntfy", [
        ("topic", "主题", "随便起个别人猜不到的名字，手机上订阅同名主题", True),
        ("server", "服务器", "默认 https://ntfy.sh", False),
        ("token", "访问令牌", "自建且开了权限才填", False)]),
    "email": ("邮件", [
        ("host", "SMTP 服务器", "如 smtp.qq.com", True),
        ("port", "端口", "默认 465（SSL）；587 走 STARTTLS", False),
        ("user", "账号", "发件邮箱", True),
        ("password", "密码/授权码", "QQ/163 邮箱要用授权码", True),
        ("to", "收件人", "多个用逗号隔开，空着就发给自己", False)]),
    "webhook": ("自定义 Webhook", [
        ("url", "地址", "每轮把命中列表以 JSON POST 过去，自己接 QQ 机器人等", True)]),
}

CHANNEL_GUIDES = {
    "wxpusher": ("https://wxpusher.zjiecode.com/docs/spt.html",
                 "个人只填 SPT。消息带大图和复制原链接按钮；想点通知直达商品，在 WxPusher 的「我的 → 通用 → 通知点击设置」选「原文链接」。"),
    "xtuis": ("https://xtuis.cn/xtuisstart.html", "用微信关注公众号获取 Token，正文和商品链接在消息详情里。"),
    "iyuu": ("https://iyuu.cn/", "用微信扫码获取令牌；同一批商品会合并发送，节省每小时额度。"),
    "pushme": ("https://push.i-i.me/docs/guide", "安卓安装 App 后获取 Push Key，并按手机型号设置后台权限。"),
    "discord": ("https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks",
                "复制自己的频道 Webhook 地址，商品会以带图卡片发到这个频道。"),
    "pushover": ("https://pushover.net/", "需要应用 API Token 和 User Key。客户端需买断，请先确认能正常使用试用版。"),
    "bark": ("https://bark.day.app/", "在 iPhone 安装 Bark，把 App 里显示的设备 Key 填进来。"),
    "feishu": ("https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot", "在自己的飞书群里添加自定义机器人并复制地址。"),
    "email": ("https://hiflow.tencent.com/document/applications/qq-mail/", "QQ 邮箱请先开启 SMTP 服务，密码一栏填授权码。"),
}


# —— 文本 ——
def cny(p: int | None, rate: float) -> str:
    return "" if p is None else f"≈{p * rate:,.0f}元"


def is_cny(source: str) -> bool:
    """闲鱼标的是人民币，别的都是日元。"""
    return getattr(SOURCES.get(source), "currency", "JPY") == "CNY"


def money(p: int | None, source: str = "") -> str:
    if p is None:
        return "价格未知"
    return f"{p:,}元" if is_cny(source) else f"¥{p:,}"


def src_name(key: str) -> str:
    s = SOURCES.get(key)
    return s.name if s else key


def headline(h: Hit, rate: float) -> str:
    it = h.item
    price = money(it.price, it.source)
    if it.price is not None and not is_cny(it.source):
        price += f"（{cny(it.price, rate)}）"
    if h.kind == "drop":
        price = f"降价 {money(h.old_price, it.source)} → {price}"
    return price


def summary_title(hits: list[Hit], ctx: Ctx | None = None) -> str:
    if ctx and ctx.message and hits:
        text = message_layout.template(ctx.message["title_template"], _message_data(hits[0], ctx), len(hits))
        return text.strip() or f"{APP_NAME} · {len(hits)} 件商品"
    names = []
    for h in hits:
        if h.watch_name not in names:
            names.append(h.watch_name)
    drops = sum(1 for h in hits if h.kind == "drop")
    news = len(hits) - drops
    parts = []
    if news:
        parts.append(f"上新 {news} 件")
    if drops:
        parts.append(f"降价 {drops} 件")
    label = "、".join(names[:3]) + ("等" if len(names) > 3 else "")
    return f"【{APP_NAME}】{label} {'，'.join(parts)}"


_MERCDN_OPTS = re.compile(r"(//static\.mercdn\.net/)c!/([^/]*)/")
_MERCDN_WEBP = {"/thumb/item/webp/": "/thumb/item/jpeg/", "/item/detail/webp/": "/item/detail/orig/"}
_SHOPS_WEBP = re.compile(r"(//assets\.mercari-shops-static\.com/.*)@webp$")


def cover(it: Item) -> str:
    """推送用的封面图地址。

    企业微信、钉钉这些只认 JPG/PNG，网页上给的 WebP 要换成 JPG 版（2026 年 9 月逐个试过）：
    煤炉 /thumb/item/webp/ → /thumb/item/jpeg/，旧写法 c!/…,f=webp/ 去掉 f=webp；
    煤炉 Shops 结尾的 @webp → @jpg；骏河屋的 photo.php 会跳到 WebP，换成 pics_light 下的 JPG。
    不是 http(s) 的地址推送渠道用不了，干脆不给。
    """
    url = it.image or ""
    if not url.startswith(("http://", "https://")):
        return ""
    if it.source == "surugaya" and ("photo.php" in url or url.endswith(".webp")):
        return f"https://www.suruga-ya.jp/database/pics_light/game/{it.id}.jpg"

    def to_jpg(m):
        opts = [o for o in m.group(2).split(",") if o and not o.startswith("f=")]
        return m.group(1) + (f"c!/{','.join(opts)}/" if opts else "")
    url = _MERCDN_OPTS.sub(to_jpg, url)
    if "//static.mercdn.net/" in url:
        for webp, jpg in _MERCDN_WEBP.items():
            url = url.replace(webp, jpg)
    return _SHOPS_WEBP.sub(r"\1@jpg", url)


_YIMG = re.compile(r"^https://auc-pctr\.c\.yimg\.jp/i/([^?]+)")
_MERCDN_THUMB = re.compile(r"^(https://static\.mercdn\.net)/thumb/item/jpeg/(m\w+_\d+\.jpg.*)$")


def photo(it: Item) -> str:
    """放大图的地方（推送页、企业微信第一篇、Telegram、Bark）用：缩略图换成各站的大图。

    2026 年 9 月逐个试过，都是 JPG：煤炉 240px → 详情页 1080px 原图；Shops small → large；
    雅虎的缩图服务改成 800px；Rakuma /m/ → /l/；Mandarake 去掉 s_；animate 缩图参数改 800。
    骏河屋没有更大的，别的站换不了就用封面。
    """
    url = cover(it)
    m = _MERCDN_THUMB.match(url)
    if m:
        return f"{m.group(1)}/item/detail/orig/photos/{m.group(2)}"
    if "//assets.mercari-shops-static.com/-/small/" in url:
        return url.replace("/-/small/", "/-/large/")
    m = _YIMG.match(url)
    if m:
        return (f"https://auc-pctr.c.yimg.jp/i/{m.group(1)}?pri=l&w=800&h=800&up=0&nf_src=sy"
                "&nf_path=images/auc/pc/top/image/1.0.3/na_170x170.png&nf_st=200")
    url = re.sub(r"(//img\.fril\.jp/img/\d+/)m/", r"\1l/", url)
    url = re.sub(r"(//img\.mandarake\.co\.jp/.*/)s_([^/]+)$", r"\1\2", url)
    return re.sub(r"(resize_image\.php\?.*?)width=\d+&height=\d+", r"\1width=800&height=800", url)


def price_tag(h: Hit) -> str:
    price = money(h.item.price, h.item.source)
    return f"降价 {price}" if h.kind == "drop" else price


PAGE_LABEL = "📄 推送页"


def page_md(ctx: Ctx) -> str:
    if ctx.message:
        return ""
    return f"[{PAGE_LABEL}]({ctx.page_url})\n\n" if ctx.page_url else ""


def page_html(ctx: Ctx) -> str:
    if ctx.message or not ctx.page_url:
        return ""
    return f'<p style="margin:0 0 12px"><a href="{html.escape(ctx.page_url)}" style="color:#c62f63">{PAGE_LABEL}</a></p>'


def short_title(title: str, n: int = 40) -> str:
    title = " ".join((title or "").split())
    return title if len(title) <= n else title[:n - 1] + "…"


def price_line(h: Hit) -> str:
    """一行里的价格：¥2,400；降价写成 ↓¥2,500（原 ¥3,000）；闲鱼写成 380元。"""
    price = money(h.item.price, h.item.source)
    if h.kind == "drop" and h.old_price is not None:
        return f"↓{price}（原 {money(h.old_price, h.item.source)}）"
    return price


def plain_lines(hits: list[Hit], ctx: Ctx, offset: int = 0) -> str:
    if ctx.message:
        return "\n\n".join(_message_body(h, ctx, i, "text") for i, h in enumerate(hits, offset))
    return "\n".join(f"{price_line(h)} {short_title(h.item.title)}（{src_name(h.item.source)}）\n{link(h, ctx, i)}"
                     for i, h in enumerate(hits, offset))


def md_line(h: Hit, ctx: Ctx, i: int) -> str:
    """markdown 里的一行：[价格 标题](去买的链接) · 网站 · [原页]（去的不是原网页时才有）"""
    if ctx.message:
        return _message_body(h, ctx, i, "markdown")
    it = h.item
    go = link(h, ctx, i)
    text = f"{price_line(h)} {short_title(it.title)}".translate(_MD_PLAIN)
    line = f"[{text}]({_md_url(go)}) · {src_name(it.source)}"
    if go != it.url and not (ctx.page_url and go.startswith(ctx.page_url)):
        line += f" · [原页]({_md_url(it.url)})"
    return line


def markdown(hits: list[Hit], ctx: Ctx) -> str:
    """Server酱、钉钉用：一件一行。行与行之间空一行，Server酱只认空行换行。"""
    return "\n\n".join(md_line(h, ctx, i) for i, h in enumerate(hits))


def html_list(hits: list[Hit], ctx: Ctx, offset: int = 0) -> str:
    """PushPlus、邮件用：一件一行，左边小图，「价格 标题」点开去买，下面一行小字是网站。"""
    if ctx.message:
        return "".join(_message_body(h, ctx, i, "html") for i, h in enumerate(hits, offset))
    esc = html.escape
    rows = []
    for i, h in enumerate(hits, offset):
        it = h.item
        go = link(h, ctx, i)
        img = cover(it)
        pic = (f'<img src="{esc(img)}" referrerpolicy="no-referrer" width="56" height="56" '
               'style="width:56px;height:56px;object-fit:cover;border-radius:6px;margin-right:10px;flex:none">' if img else "")
        orig = f' · <a href="{esc(it.url)}" style="color:#999">原页</a>' if go != it.url else ""
        rows.append(
            f'<div style="display:flex;align-items:center;margin:0 0 10px">{pic}<div>'
            f'<a href="{esc(go)}" style="color:#222;text-decoration:none;font-size:14px">'
            f'<b style="color:#e0457b">{esc(price_line(h))}</b> {esc(short_title(it.title, 60))}</a>'
            f'<div style="font-size:12px;color:#999">{esc(src_name(it.source))}{orig}</div></div></div>')
    return "".join(rows)


def _wxpusher_item(h: Hit, ctx: Ctx, i: int) -> str:
    """WxPusher 原生 copy 标签可在消息正文复制，不需要手机连回本机网页。"""
    if ctx.message:
        return _message_body(h, ctx, i, "html")
    esc = html.escape
    it = h.item
    original = it.url if it.url.startswith(("http://", "https://")) else ""
    go = link(h, ctx, i)
    go = go if go.startswith(("http://", "https://")) else original
    doorzo = buy_url(it, "doorzo")
    doorzo = doorzo if doorzo != it.url and doorzo.startswith("https://") else ""
    image = photo(it) or cover(it)
    picture = ""
    if image:
        picture = (f'<img src="{esc(image)}" referrerpolicy="no-referrer" width="320" '
                   'style="display:block;width:100%;max-width:360px;height:auto;border-radius:12px;margin:0 0 12px">')
        if go:
            picture = f'<a href="{esc(go)}">{picture}</a>'
    title = esc(short_title(it.title, 160))
    if go:
        title = f'<a href="{esc(go)}" style="color:#222;text-decoration:none">{title}</a>'
    meta = " · ".join(p for p in (src_name(it.source), short_title(h.watch_name, 80), short_title(it.extra, 120)) if p)
    button = ('display:inline-block;padding:10px 14px;margin:0 8px 8px 0;border-radius:8px;'
              'font-size:14px;font-weight:600;line-height:1.4;text-decoration:none;')
    actions = []
    if doorzo:
        actions.append(f'<a href="{esc(doorzo)}" style="{button}background:#e0457b;color:#fff">去任意门</a>')
    if original:
        actions.append(f'<copy data-clipboard-text="{esc(original)}" '
                       f'style="{button}background:#edf2f8;color:#285887;cursor:pointer">复制原链接</copy>')
        actions.append(f'<a href="{esc(original)}" style="{button}background:#f3f3f3;color:#444">原商品页</a>')
    return ('<div style="margin:0 0 20px;padding:0 0 16px;border-bottom:1px solid #e8e8e8">'
            f'{picture}<p style="margin:0 0 8px;font-size:17px;line-height:1.5;font-weight:600">{title}</p>'
            f'<p style="margin:0 0 6px;font-size:20px;font-weight:700;color:#d0346b">{esc(headline(h, ctx.rate))}</p>'
            f'<p style="margin:0 0 12px;font-size:12px;line-height:1.6;color:#777">{esc(meta)}</p>'
            f'<div>{"".join(actions)}</div></div>')


# —— 渠道 ——
def _expect(resp: net.Response, name: str, ok) -> None:
    resp.check(name)
    try:
        data = resp.json()
    except ValueError:
        return
    if not ok(data):
        raise RuntimeError(f"{name} 返回：{str(data)[:200]}")


def send_serverchan(ch, hits, ctx):
    key = ch["sendkey"]
    m = re.match(r"^sctp(\d+)t", key)
    url = (f"https://{m.group(1)}.push.ft07.com/send/{key}.send" if m
           else f"https://sctapi.ftqq.com/{key}.send")
    if ctx.message:
        for start, batch, content in _message_batches(hits, ctx, "markdown", limit=8000, max_bytes=24000):
            resp = net.post(url, json_body={"title":summary_title(batch, ctx)[:32], "desp":content,
                                            "short":plain_lines(batch, ctx, start)[:64]})
            _expect(resp, "Server酱", lambda d: d.get("code") in (0, 200))
        return
    # short 是微信里那张消息卡片上显示的字
    short = (plain_lines(hits[:3], ctx) if ctx.message else
             "；".join(f"{price_line(h)} {short_title(h.item.title, 20)}" for h in hits[:3]))
    resp = net.post(url, json_body={"title": summary_title(hits, ctx)[:32],
                                    "desp": page_md(ctx) + markdown(hits, ctx), "short": short[:64]})
    _expect(resp, "Server酱", lambda d: d.get("code") in (0, 200))


def send_pushplus(ch, hits, ctx):
    if ctx.message:
        for _, batch, content in _message_batches(hits, ctx, "html", limit=8000, max_bytes=16000):
            body = {"token":ch["token"], "title":summary_title(batch, ctx), "content":content, "template":"html"}
            if ch.get("topic"):
                body["topic"] = ch["topic"]
            _expect(net.post("https://www.pushplus.plus/send", json_body=body), "PushPlus", lambda d: d.get("code") == 200)
        return
    body = {"token": ch["token"], "title": summary_title(hits, ctx),
            "content": page_html(ctx) + html_list(hits, ctx), "template": "html"}
    if ch.get("topic"):
        body["topic"] = ch["topic"]
    resp = net.post("https://www.pushplus.plus/send", json_body=body)
    _expect(resp, "PushPlus", lambda d: d.get("code") == 200)


def _result(resp: net.Response, name: str, ok) -> dict:
    """需要明确的业务成功码；登录页、空响应不能当成发送成功。"""
    resp.check(name)
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"{name}没有返回可识别的发送结果") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"{name}没有返回可识别的发送结果")
    if not ok(data):
        why = data.get("msg") or data.get("errmsg") or data.get("errors") or data.get("message") or "接口拒绝了消息"
        raise RuntimeError(f"{name}没发成功：{str(why)[:200]}")
    return data


def _values(value) -> list[str]:
    parts = value if isinstance(value, (list, tuple)) else re.split(r"[,，;；\s]+", str(value or ""))
    return list(dict.fromkeys(str(p).strip() for p in parts if str(p).strip()))


def _wx_targets(ch: dict) -> tuple[str, dict]:
    token = ch["token"]
    if token.startswith("SPT_"):
        spts = _values(token)
        if len(spts) > 10 or any(not s.startswith("SPT_") or len(s) <= 4 for s in spts):
            raise ValueError("WxPusher 最多填 10 个 SPT 令牌，多个用逗号隔开")
        return "simple-push", {"spt": spts[0]} if len(spts) == 1 else {"sptList": spts}
    if not token.startswith("AT_") or len(token) <= 3:
        raise ValueError("WxPusher 令牌要以 SPT_ 或 AT_ 开头，自己用建议扫码获取 SPT")
    uids, raw_topics = _values(ch.get("uids")), _values(ch.get("topic_ids"))
    try:
        topics = list(dict.fromkeys(int(t) for t in raw_topics))
    except ValueError:
        raise ValueError("WxPusher 主题 ID 要填正整数，多个用逗号隔开") from None
    if any(t <= 0 for t in topics):
        raise ValueError("WxPusher 主题 ID 要填正整数")
    if not uids and not topics:
        raise ValueError("使用 WxPusher 应用令牌时，要填写接收 UID 或主题 ID；只给自己发可改用 SPT")
    if len(uids) > 2000 or len(topics) > 5:
        raise ValueError("WxPusher 一次最多 2000 个 UID、5 个主题 ID")
    targets = {"appToken": token}
    if uids:
        targets["uids"] = uids
    if topics:
        targets["topicIds"] = topics
    return "", targets


def _message_batches(hits: list[Hit], ctx: Ctx, style: str, limit: int = 39000, max_bytes: int | None = 60000):
    """按整件商品拆消息，保留原来的推送页序号；同时遵守字符数和 UTF-8 字节数限制。"""
    if style in ("html", "wxpusher"):
        prefix, separator = page_html(ctx), ""
        render = lambda h, i: _wxpusher_item(h, ctx, i) if style == "wxpusher" else html_list([h], ctx, i)
    elif style == "telegram":
        prefix, separator = "", "\n\n"
        render = lambda h, i: _message_body(h, ctx, i, "telegram")
    elif style == "markdown":
        prefix, separator = page_md(ctx).strip(), "\n\n"
        render = lambda h, i: md_line(h, ctx, i)
    else:
        prefix = f"{PAGE_LABEL}\n{ctx.page_url}" if ctx.page_url and not ctx.message else ""
        separator = "\n\n"
        render = lambda h, i: plain_lines([h], ctx, i)

    def fits(text):
        return len(text) <= limit and (max_bytes is None or len(text.encode("utf-8")) <= max_bytes)

    start, batch, parts = 0, [], [prefix] if prefix else []
    max_items = ctx.message.get("max_items", 0) if ctx.message else 0
    for i, h in enumerate(hits):
        part = render(h, i)
        if (max_items and len(batch) >= max_items) or not fits(separator.join([*parts, part])):
            if batch:
                yield start, batch, separator.join(parts)
            start, batch, parts = i, [], [prefix] if prefix else []
            if not fits(separator.join([*parts, part])):
                if ctx.message:
                    format = "html" if style in ("html", "wxpusher") else "markdown" if style == "markdown" else "telegram" if style == "telegram" else "text"
                    fragments = message_layout.render(_message_data(h, ctx, i), ctx.message, ctx.channel_kind, format, parts_only=True)
                    chunk = ""
                    for fragment in fragments:
                        if not fits(fragment):
                            raise ValueError("单个消息模块太长，请缩短自定义文案或商品链接")
                        candidate = chunk + (separator if chunk else "") + fragment
                        if chunk and not fits(candidate):
                            yield i, [h], chunk
                            chunk = fragment
                        else:
                            chunk = candidate
                    if chunk:
                        yield i, [h], chunk
                    batch, parts = [], [prefix] if prefix else []
                    continue
                raise ValueError("单件商品内容超过推送长度限制，请检查商品链接或手机访问地址")
        if not batch:
            start = i
        batch.append(h)
        parts.append(part)
    if batch:
        yield start, batch, separator.join(parts)


def send_wxpusher(ch, hits, ctx):
    route, targets = _wx_targets(ch)
    url = "https://wxpusher.zjiecode.com/api/send/message" + (f"/{route}" if route else "")
    # 原生复制标签会被服务端处理，给处理后的 HTML 留出余量。
    for start, batch, content in _message_batches(hits, ctx, "wxpusher", limit=30000, max_bytes=45000):
        body = {**targets, "content": content, "contentType": 2, "summary": short_title(summary_title(batch, ctx), 100)}
        go = link(batch[0], ctx, start)
        if len(batch) > 1 and ctx.page_url and go == f"{ctx.page_url}#i{ctx.item_offset + start + 1}":
            go = ctx.page_url
        if go and len(go) <= 1000:
            body["url"] = go
        result = _result(net.post(url, json_body=body, retries=0), "WxPusher", lambda d: d.get("code") == 1000)
        for target in result.get("data") or []:
            if isinstance(target, dict) and target.get("code") not in (None, 1000):
                raise RuntimeError(f"WxPusher 有接收人没发成功：{str(target.get('status') or target.get('code'))[:180]}")


def send_xtuis(ch, hits, ctx):
    url = f"https://wx.xtuis.cn/{urllib.parse.quote(ch['token'], safe='')}.send"
    for _, batch, content in _message_batches(hits, ctx, "markdown", limit=60000):
        resp = net.post(url, data={"text": short_title(summary_title(batch, ctx), 100), "desp": content}, retries=0)
        _result(resp, "虾推啥", lambda d:
                str(d.get("code", 0)) in ("0", "200") and str(d.get("errcode", 0)) == "0"
                and ("code" in d or "errcode" in d or d.get("success") is True or bool(d.get("msg_id"))))


def send_iyuu(ch, hits, ctx):
    url = f"https://iyuu.cn/{urllib.parse.quote(ch['token'], safe='')}.send"
    for _, batch, content in _message_batches(hits, ctx, "markdown", limit=60000):
        resp = net.post(url, data={"text": short_title(summary_title(batch, ctx), 39), "desp": content}, retries=0)
        _result(resp, "爱语飞飞", lambda d: d.get("errcode") == 0)


def send_pushme(ch, hits, ctx):
    url = (ch.get("server") or "https://push.i-i.me").rstrip("/")
    for _, batch, content in _message_batches(hits, ctx, "html"):
        resp = net.post(url, data={"push_key": ch["push_key"], "title": summary_title(batch, ctx),
                                  "content": content, "type": "html"}, retries=0).check("PushMe")
        if resp.text().strip() != "success":
            raise RuntimeError(f"PushMe 没发成功：{resp.text().strip()[:200] or '接口返回空内容'}")


def send_discord(ch, hits, ctx):
    # wait=true 才会确认消息已保存；保留地址里原有的 thread_id 等参数。
    parts = urllib.parse.urlsplit(ch["webhook"])
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query["wait"] = "true"
    url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    if ctx.message:
        pending, batch, cost = [], [], 0
        maximum = min(10, ctx.message["max_items"] or 10)

        def flush():
            body = {"content": short_title(summary_title(batch, ctx), 160), "embeds": list(pending),
                    "allowed_mentions": {"parse": []}}
            _discord_post(url, body)

        for i, h in enumerate(hits):
            description = _message_body(h, ctx, i, "markdown")
            if len(description.encode("utf-16-le")) // 2 > 4096:
                raise ValueError("Discord 单件消息太长，请缩短自定义文案或减少模块")
            embed = {"title": short_title(summary_title([h], ctx), 120), "description": description,
                     "url": link(h, ctx, i)}
            picture = _message_photo(h.item, ctx)
            if picture:
                embed["thumbnail" if ctx.message["image_size"] == "small" else "image"] = {"url": picture}
            size = len((embed["title"] + description).encode("utf-16-le")) // 2
            if pending and (len(pending) >= maximum or cost + size > 5800):
                flush()
                pending, batch, cost = [], [], 0
            pending.append(embed)
            batch.append(h)
            cost += size
        if pending:
            flush()
        return
    for start in range(0, len(hits), 10):
        batch = hits[start:start + 10]
        content = short_title(summary_title(batch, ctx), 160)
        page_link = f"\n[{PAGE_LABEL}]({_md_url(ctx.page_url)})" if ctx.page_url else ""
        if len((content + page_link).encode("utf-16-le")) <= 4000:
            content += page_link
        embeds = []
        for i, h in enumerate(batch, start):
            it = h.item
            go = link(h, ctx, i)
            # 用较短的文字，也给 emoji 占两个 UTF-16 单元的情况留出空间。
            embed = {"title": short_title(f"{price_line(h)} {it.title}", 120), "url": go,
                     "footer": {"text": short_title(f"{src_name(it.source)} · {h.watch_name}", 60)}}
            if go != it.url:
                embed["author"] = {"name": "原商品页", "url": it.url}
            if it.extra:
                embed["description"] = short_title(it.extra, 60).translate(_MD_PLAIN)
            if cover(it):
                embed["thumbnail"] = {"url": cover(it)}
            embeds.append(embed)
        body = {"content": content, "embeds": embeds, "allowed_mentions": {"parse": []}}
        _discord_post(url, body)


def _discord_post(url, body):
    for attempt in range(3):
        resp = net.post(url, json_body=body, retries=0)
        if resp.status != 429 or attempt == 2:
            resp.check("Discord")
            return
        try:
            delay = float(resp.json()["retry_after"])
        except (ValueError, TypeError, KeyError):
            resp.check("Discord")
        if not 0 <= delay <= 10:
            resp.check("Discord")
        time.sleep(delay + 0.05)


def send_pushover(ch, hits, ctx):
    if ctx.message:
        messages = []
        for i, h in enumerate(hits):
            text = plain_lines([h], ctx, i)
            while text:
                end = text.rfind("\n", 0, 1025) if len(text) > 1024 else len(text)
                end = end if end > 0 else min(1024, len(text))
                messages.append((i, [h], text[:end]))
                text = text[end:].lstrip("\n")
    else:
        messages = _message_batches(hits, ctx, "text", limit=1024, max_bytes=None)
    for start, batch, content in messages:
        body = {"token": ch["token"], "user": ch["user_key"], "title": short_title(summary_title(batch, ctx), 250),
                "message": content}
        for key in ("device", "sound"):
            if ch.get(key):
                body[key] = ch[key]
        go = link(batch[0], ctx, start)
        if len(batch) > 1 and ctx.page_url and (not ctx.message or go == f"{ctx.page_url}#i{ctx.item_offset + start + 1}"):
            go = ctx.page_url
        if go and len(go) <= 512:
            body.update(url=go, url_title="推送页" if go == ctx.page_url else "打开商品")
        resp = net.post("https://api.pushover.net/1/messages.json", data=body, retries=0)
        _result(resp, "Pushover", lambda d: d.get("status") == 1)


# markdown 里有特殊含义的字符换成全角，标题里出现也不会把格式弄乱
_MD_PLAIN = str.maketrans({"[": "［", "]": "］", "*": "＊", "_": "＿", "`": "｀", "|": "｜", "<": "＜", ">": "＞",
                           "#": "＃", "~": "～"})


def _md_url(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def wecom_list(hits: list[Hit], ctx: Ctx, limit: int = 4000) -> list[str]:
    """企业微信：开头一行粗体说是哪几个关注上新了几件，下面一件一行。一条最多 4096 字节，放不下就拆。"""
    head = f"**{summary_title(hits, ctx).replace(f'【{APP_NAME}】', '').strip().translate(_MD_PLAIN)}**"
    if ctx.message:
        head = f"**{short_title(summary_title(hits, ctx).translate(_MD_PLAIN), 110)}**"
        return [head + "\n\n" + content for _, _, content in
                _message_batches(hits, ctx, "markdown", limit=3000, max_bytes=3500)]
    if ctx.page_url:
        head += f" · [{PAGE_LABEL}]({_md_url(ctx.page_url)})"
    out, cur = [], head
    for i, h in enumerate(hits):
        line = md_line(h, ctx, i)
        if len(f"{cur}\n\n{line}".encode("utf-8")) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n\n{line}"
    out.append(cur)
    return out


def on(ch: dict, key: str) -> bool:
    """开关类的字段：没填就是开。"""
    return str(ch.get(key, True)).strip().lower() not in ("false", "0", "off", "no", "")


def _wecom_rejects_type(err: RuntimeError) -> bool:
    """企业微信接口说不认识这种消息（老接口没有 markdown_v2）。"""
    msg = str(err).lower()
    return "40008" in msg or "msgtype" in msg or "message type" in msg


def wecom_cards(ch: dict) -> bool:
    if "cards" in ch:
        return on(ch, "cards")
    return "rich" in ch and not on(ch, "rich")   # 0.3 版「一条消息」开关关掉时，用的就是卡片


def send_wecom(ch, hits, ctx):
    if ctx.message or not wecom_cards(ch):
        # 先用 markdown_v2，接口不认（老版本）就用 markdown，再不认才退回图文卡片
        for msgtype in ("markdown_v2", "markdown"):
            for n, md in enumerate(wecom_list(hits, ctx)):
                resp = net.post(ch["webhook"], json_body={"msgtype": msgtype, msgtype: {"content": md}})
                try:
                    _expect(resp, "企业微信", lambda d: d.get("errcode") == 0)
                except RuntimeError as e:
                    if n == 0 and _wecom_rejects_type(e):
                        break
                    raise
            else:
                return
    # 图文卡片一条最多 8 篇，第一篇大图，后面小图；只有一篇时才显示描述，所以价格写进标题
    for i in range(0, len(hits), 8):
        articles = [{
            "title": short_title(summary_title([h], ctx), 120) if ctx.message else f"{price_line(h)} {h.item.title}"[:120],
            "description": plain_lines([h], ctx, i + j)[:500] if ctx.message else f"{src_name(h.item.source)} · {h.watch_name}",
            "url": link(h, ctx, i + j), "picurl": _message_photo(h.item, ctx) if ctx.message else photo(h.item) if j == 0 else cover(h.item),
        } for j, h in enumerate(hits[i:i + 8])]
        resp = net.post(ch["webhook"], json_body={"msgtype": "news", "news": {"articles": articles}})
        _expect(resp, "企业微信", lambda d: d.get("errcode") == 0)


def send_dingtalk(ch, hits, ctx):
    url = ch["webhook"]
    if ch.get("secret"):
        ts = str(int(time.time() * 1000))
        digest = hmac.new(ch["secret"].encode(), f"{ts}\n{ch['secret']}".encode(), hashlib.sha256).digest()
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {"timestamp": ts, "sign": base64.b64encode(digest).decode()})
    messages = _message_batches(hits, ctx, "markdown", limit=7000, max_bytes=16000) if ctx.message else [(0, hits, page_md(ctx) + markdown(hits, ctx))]
    for _, batch, content in messages:
        title = summary_title(batch, ctx)
        resp = net.post(url, json_body={"msgtype": "markdown", "markdown": {
            "title":title, "text":f"### {title[:200].translate(_MD_PLAIN)}\n\n{content}"}})
        _expect(resp, "钉钉", lambda d: d.get("errcode") == 0)


def send_feishu(ch, hits, ctx):
    content = [[{"tag": "a", "text": PAGE_LABEL, "href": ctx.page_url}]] if ctx.page_url and not ctx.message else []
    for i, h in enumerate(hits):
        if ctx.message:
            for part in message_layout.sections(_message_data(h, ctx, i), ctx.message):
                if part.get("url"):
                    label = part["text"].replace("复制", "") + "（长按复制）" if part["kind"] == "copy" else part["text"]
                    content.append([{"tag": "a", "text": label, "href": part["url"]}])
                else:
                    content.append([{"tag": "text", "text": part["text"]}])
            continue
        it = h.item
        go = link(h, ctx, i)
        # 飞书富文本只能放上传过的图，外链图片放不进来，所以这里只有文字和链接
        row = [{"tag": "a", "text": f"{price_line(h)} {short_title(it.title)}", "href": go},
               {"tag": "text", "text": f" · {src_name(it.source)}"}]
        if go != it.url:
            row.append({"tag": "a", "text": " 原页", "href": it.url})
        content.append(row)
    body = {"msg_type": "post", "content": {"post": {"zh_cn": {
        "title": summary_title(hits, ctx), "content": content}}}}
    if ch.get("secret"):
        ts = str(int(time.time()))
        digest = hmac.new(f"{ts}\n{ch['secret']}".encode(), b"", hashlib.sha256).digest()
        body.update({"timestamp": ts, "sign": base64.b64encode(digest).decode()})
    resp = net.post(ch["webhook"], json_body=body)
    _expect(resp, "飞书", lambda d: d.get("code", d.get("StatusCode")) == 0)


def _per_item(hits):
    return hits[:PER_ITEM_LIMIT], hits[PER_ITEM_LIMIT:]


def _system_groups(hits, ctx):
    """前几件带系统配图；余下的合并内容，配图改成链接，保留所有所选信息。"""
    single, rest = _per_item(hits)
    for i, h in enumerate(single):
        for text in _text_chunks(plain_lines([h], ctx, i), 3000, 3800):
            yield i, [h], text
    if rest:
        more_ctx = replace(ctx, channel_kind="webhook")
        batch, content, start = [], "", len(single)
        for i, h in enumerate(rest, len(single)):
            for text in _text_chunks(plain_lines([h], more_ctx, i), 3000, 3800):
                combined = content + ("\n\n" if content else "") + text
                if batch and (len(combined) > 3000 or len(combined.encode("utf-8")) > 3800):
                    yield start, batch, content
                    batch, content, start = [], "", i
                if not batch:
                    start = i
                batch.append(h)
                content += ("\n\n" if content else "") + text
        if batch:
            yield start, batch, content


def _text_chunks(text, limit, max_bytes):
    """系统通知按换行分段；一个超长字段也不会超过渠道字节上限。"""
    while text:
        end = min(limit, len(text))
        while len(text[:end].encode("utf-8")) > max_bytes:
            end -= 1
        if end < len(text):
            newline = text.rfind("\n", 0, end + 1)
            if newline > 0:
                end = newline
        yield text[:end]
        text = text[end:].lstrip("\n")


def send_bark(ch, hits, ctx):
    server = (ch.get("server") or "https://api.day.app").rstrip("/")
    if ctx.message:
        for i, batch, text in _system_groups(hits, ctx):
            h = batch[0]
            message = {"device_key": ch["key"], "group": APP_NAME,
                       "title": short_title(summary_title(batch, ctx), 200), "body": text, "url": link(h, ctx, i)}
            picture = _message_photo(h.item, ctx) if len(batch) == 1 else ""
            if picture:
                message.update(icon=cover(h.item), image=picture)
            if "copy_original" in ctx.message["blocks"]:
                message["copy"] = h.item.url
            elif "copy_buy" in ctx.message["blocks"] and buy_url(h.item, "doorzo") != h.item.url:
                message["copy"] = buy_url(h.item, "doorzo")
            _expect(net.post(f"{server}/push", json_body=message), "Bark", lambda d: d.get("code") == 200)
        return
    single, rest = _per_item(hits)
    msgs = [{"title": f"{price_line(h)} · {src_name(h.item.source)}",
             "body": short_title(h.item.title, 80),
             "url": link(h, ctx, i), "icon": cover(h.item), "image": photo(h.item),
             "copy": h.item.url} for i, h in enumerate(single)]   # 长按通知复制的是原链接
    if rest:
        msgs.append({"title": f"还有 {len(rest)} 件", "body": summary_title(rest, ctx),
                     "url": link(rest[0], ctx, len(single)) if ctx.page_url else ctx.web_url or rest[0].item.url})
    for m in msgs:
        m.update({"device_key": ch["key"], "group": APP_NAME})
        resp = net.post(f"{server}/push", json_body=m)
        _expect(resp, "Bark", lambda d: d.get("code") == 200)


def send_ntfy(ch, hits, ctx):
    server = (ch.get("server") or "https://ntfy.sh").rstrip("/")
    headers = {"Authorization": f"Bearer {ch['token']}"} if ch.get("token") else None
    if ctx.message:
        for i, batch, text in _system_groups(hits, ctx):
            h = batch[0]
            message = {"topic": ch["topic"], "title": short_title(summary_title(batch, ctx), 200),
                       "message": text, "click": link(h, ctx, i)}
            picture = _message_photo(h.item, ctx) if len(batch) == 1 else ""
            if picture:
                message["attach"] = picture
            actions = [{"action": "view", "label": p["text"], "url": p["url"]}
                       for p in message_layout.sections(_message_data(h, ctx, i), ctx.message) if p["kind"] == "link"]
            if actions:
                message["actions"] = actions[:3]
            net.post(server, json_body=message, headers=headers).check("ntfy")
        return
    single, rest = _per_item(hits)
    msgs = [{"title": f"{price_line(h)} · {src_name(h.item.source)}",
             "message": short_title(h.item.title, 80),
             "click": link(h, ctx, i), "attach": photo(h.item) or None,
             # 点通知去的不是原网页（任意门、推送页）时，再给一个直达原商品页的按钮
             "actions": ([{"action": "view", "label": "原商品页", "url": h.item.url}]
                         if link(h, ctx, i) != h.item.url else None)}
            for i, h in enumerate(single)]
    if rest:
        msgs.append({"title": f"还有 {len(rest)} 件", "message": summary_title(rest, ctx),
                     "click": link(rest[0], ctx, len(single)) if ctx.page_url else ctx.web_url or rest[0].item.url})
    for m in msgs:
        m = {k: v for k, v in m.items() if v}
        m["topic"] = ch["topic"]
        net.post(server, json_body=m, headers=headers).check("ntfy")


def send_telegram(ch, hits, ctx):
    api = (ch.get("api") or "https://api.telegram.org").rstrip("/")
    base = f"{api}/bot{ch['token']}"
    if ctx.message:
        single, rest = _per_item(hits)
        groups = [(i, [h]) for i, h in enumerate(single)]
        if rest:
            groups.append((len(single), rest))
        for start, batch in groups:
            parts = []
            for i, h in enumerate(batch, start):
                parts.extend(message_layout.render(_message_data(h, ctx, i), ctx.message, "telegram", "telegram", parts_only=True))
            chunks, current = [], ""
            for part in parts:
                if len(part) > 3800:
                    raise ValueError("Telegram 单个模块太长，请缩短文案")
                if current and len(current + "\n" + part) > 3800:
                    chunks.append(current)
                    current = ""
                current = current + ("\n" if current else "") + part
            if current:
                chunks.append(current)
            picture = _message_photo(batch[0].item, ctx) if len(batch) == 1 else ""
            if picture and len(chunks) == 1 and len(chunks[0]) <= 1000:
                response = net.post(f"{base}/sendPhoto", json_body={"chat_id": ch["chat_id"], "photo": picture,
                                    "caption": chunks[0], "parse_mode": "HTML"})
                if response.ok:
                    _expect(response, "Telegram", lambda d: d.get("ok") is True)
                    continue
            for chunk in chunks:
                response = net.post(f"{base}/sendMessage", json_body={"chat_id": ch["chat_id"], "text": chunk, "parse_mode": "HTML"})
                _expect(response, "Telegram", lambda d: d.get("ok") is True)
        return
    single, rest = _per_item(hits)
    for i, h in enumerate(single):
        it = h.item
        go = link(h, ctx, i)
        orig = f'  <a href="{html.escape(it.url)}">原页</a>' if go != it.url else ""
        caption = (f'<a href="{html.escape(go)}">{html.escape(price_line(h))} {html.escape(short_title(it.title, 80))}</a>\n'
                   f"{html.escape(src_name(it.source))} · {html.escape(h.watch_name)}{orig}")
        sent = False
        if photo(it):
            resp = net.post(f"{base}/sendPhoto", json_body={
                "chat_id": ch["chat_id"], "photo": photo(it), "caption": caption, "parse_mode": "HTML"})
            sent = resp.ok
        if not sent:
            resp = net.post(f"{base}/sendMessage", json_body={
                "chat_id": ch["chat_id"], "text": caption, "parse_mode": "HTML"})
            _expect(resp, "Telegram", lambda d: d.get("ok") is True)
    if rest:
        resp = net.post(f"{base}/sendMessage", json_body={
            "chat_id": ch["chat_id"], "text": f"{summary_title(rest, ctx)}\n\n{plain_lines(rest, ctx, len(single))}"[:4000],
            "disable_web_page_preview": True})
        _expect(resp, "Telegram", lambda d: d.get("ok") is True)


def send_email(ch, hits, ctx):
    port = int(ch.get("port") or 465)
    user = ch["user"]
    to = [a.strip() for a in (ch.get("to") or user).replace("，", ",").split(",") if a.strip()]
    msg = MIMEText(f'<div style="font-family:sans-serif;max-width:560px">{page_html(ctx)}{html_list(hits, ctx)}</div>',
                   "html", "utf-8")
    msg["Subject"] = Header(summary_title(hits, ctx), "utf-8")
    msg["From"] = formataddr((APP_NAME, user))
    msg["To"] = ", ".join(to)
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(ch["host"], port, context=context, timeout=20)
    else:
        server = smtplib.SMTP(ch["host"], port, timeout=20)
        server.starttls(context=context)
    try:
        server.login(user, ch["password"])
        server.sendmail(user, to, msg.as_string())
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass


def send_webhook(ch, hits, ctx):
    body = {"title": summary_title(hits, ctx), "text": plain_lines(hits, ctx), "page_url": ctx.page_url, "items": [
        {**h.item.to_dict(), "source_name": src_name(h.item.source), "watch": h.watch_name,
         "kind": h.kind, "old_price": h.old_price, "cover": cover(h.item), "photo": photo(h.item),
         "link": link(h, ctx, i), "buy_url": buy_url(h.item, ctx.buy),
         "currency": "CNY" if is_cny(h.item.source) else "JPY",
         "price_cny": (None if h.item.price is None else h.item.price if is_cny(h.item.source)
                       else round(h.item.price * ctx.rate, 1))}
        for i, h in enumerate(hits)]}
    net.post(ch["url"], json_body=body).check("Webhook")


SENDERS = {
    "serverchan": send_serverchan, "pushplus": send_pushplus, "wecom": send_wecom,
    "bark": send_bark, "dingtalk": send_dingtalk, "feishu": send_feishu,
    "telegram": send_telegram, "ntfy": send_ntfy, "email": send_email, "webhook": send_webhook,
    "wxpusher": send_wxpusher, "xtuis": send_xtuis, "iyuu": send_iyuu,
    "pushme": send_pushme, "discord": send_discord, "pushover": send_pushover,
}


def missing_fields(ch: dict) -> list[str]:
    spec = CHANNEL_TYPES.get(ch.get("type"))
    if not spec:
        return ["type"]
    return [f[1] for f in spec[1] if f[3] and not ch.get(f[0])]


def validate_channel(ch: dict) -> None:
    missing = missing_fields(ch)
    if missing:
        raise ValueError(f"没填：{'、'.join(missing)}")
    if ch.get("type") == "wxpusher":
        _wx_targets(ch)
    if ch.get("type") == "pushme" and ch.get("server"):
        parts = urllib.parse.urlsplit(ch["server"])
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("PushMe 服务器要填完整的 http:// 或 https:// 地址")


def send(ch: dict, hits: list[Hit], ctx: Ctx) -> None:
    fn = SENDERS.get(ch.get("type"))
    if fn is None:
        raise ValueError(f"不认识的推送方式：{ch.get('type')}")
    validate_channel(ch)
    effective = message_layout.effective(ctx.message, ch)
    maximum = effective["max_items"] if effective else 0
    if maximum and len(hits) > maximum:
        for start in range(0, len(hits), maximum):
            fn(ch, hits[start:start + maximum], replace(ctx, message=effective, channel_kind=ch["type"],
                                                      item_offset=ctx.item_offset + start))
    else:
        fn(ch, hits, replace(ctx, message=effective, channel_kind=ch["type"]))


def send_all(channels: list[dict], hits: list[Hit], ctx: Ctx) -> list[tuple[str, str]]:
    """返回失败列表 [(渠道名, 原因)]。"""
    failures = []
    for ch in channels:
        if not ch.get("enabled", True):
            continue
        try:
            send(ch, hits, ctx)
        except Exception as e:  # 一个渠道挂了不影响别的
            failures.append((ch.get("name") or CHANNEL_TYPES.get(ch.get("type"), (ch.get("type"),))[0], str(e)))
    return failures


def sample_hits() -> list[Hit]:
    """实在拿不到真实商品时才用：没有封面，链接也只是煤炉首页。"""
    item = Item(source="mercari", id="m00000000000", price=1200, url="https://jp.mercari.com/",
                title="【示例】五条悟 缶バッジ（还没抓到过真实商品，所以没有封面和商品链接）")
    return [Hit(item=item, watch_id="test", watch_name="测试推送")]
