"""推送渠道。每种渠道一个函数：(渠道配置, 命中列表, 上下文) → 抛异常表示失败。

一轮扫描的所有命中合成一次推送（Server酱免费版一天只有 5 条，省着用）；
Bark / ntfy / Telegram 这类手机通知逐条发，点开直达商品页，超出上限的合成一条汇总。
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
from dataclasses import dataclass
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from . import APP_NAME, net
from .sources import SOURCES, Item

PER_ITEM_LIMIT = 6   # 逐条推送的渠道，一轮最多单独推几条


@dataclass
class Hit:
    item: Item
    watch_id: str
    watch_name: str
    kind: str = "new"              # new 上新 / drop 降价
    old_price: int | None = None


@dataclass
class Ctx:
    rate: float                    # 1 日元 = ? 人民币
    web_url: str = ""


# 每种渠道需要填的字段，网页的表单按这个生成。(字段, 标签, 说明, 是否必填)
CHANNEL_TYPES = {
    "serverchan": ("Server酱（微信）", [
        ("sendkey", "SendKey", "sct.ftqq.com 登录后获取；Server酱³ 的 sctp 开头的 key 也行", True)]),
    "pushplus": ("PushPlus（微信）", [
        ("token", "Token", "www.pushplus.plus 登录后获取", True),
        ("topic", "群组编码", "一对多推送时填，可空", False)]),
    "wecom": ("企业微信群机器人", [
        ("webhook", "Webhook 地址", "群设置 → 群机器人 → 添加，复制 Webhook 地址", True)]),
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


# —— 文本 ——
def yen(p: int | None) -> str:
    return "价格未知" if p is None else f"¥{p:,}"


def cny(p: int | None, rate: float) -> str:
    return "" if p is None else f"≈{p * rate:,.0f}元"


def src_name(key: str) -> str:
    s = SOURCES.get(key)
    return s.name if s else key


def headline(h: Hit, rate: float) -> str:
    it = h.item
    price = f"{yen(it.price)}（{cny(it.price, rate)}）"
    if h.kind == "drop":
        price = f"降价 {yen(h.old_price)} → {price}"
    return price


def summary_title(hits: list[Hit]) -> str:
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


def plain_lines(hits: list[Hit], rate: float) -> str:
    out = []
    for h in hits:
        it = h.item
        extra = f" · {it.extra}" if it.extra else ""
        out.append(f"【{src_name(it.source)}】{it.title}\n{headline(h, rate)}{extra}\n{it.url}")
    return "\n\n".join(out)


def markdown(hits: list[Hit], rate: float, images: bool = True) -> str:
    """Server酱、钉钉用。

    段与段之间都空一行：Server酱只认空行换行。来源名用【】，不和链接的方括号挤在一起；
    商品链接单独成行，一眼能找到。
    """
    out = []
    for h in hits:
        it = h.item
        title = it.title.replace("[", "［").replace("]", "］").replace("*", "＊")
        meta = " · ".join(p for p in (headline(h, rate), it.extra, f"关注：{h.watch_name}") if p)
        parts = [f"**【{src_name(it.source)}】{title}**", meta]
        img = cover(it)
        if images and img:
            parts.append(f"![]({img})")
        parts.append(f"[👉 打开商品页]({it.url})")
        out.append("\n\n".join(parts))
    return "\n\n---\n\n".join(out)


def html_list(hits: list[Hit], rate: float) -> str:
    """PushPlus、邮件用：大封面在上，图、标题、按钮都能点进商品页。"""
    esc = html.escape
    rows = []
    for h in hits:
        it = h.item
        link = esc(it.url)
        img = cover(it)
        pic = (f'<a href="{link}"><img src="{esc(img)}" referrerpolicy="no-referrer" '
               'style="display:block;width:100%;max-width:320px;border-radius:8px;margin:0 0 8px"></a>'
               if img else "")
        extra = f" · {esc(it.extra)}" if it.extra else ""
        rows.append(
            '<div style="margin:0 0 18px;padding-bottom:14px;border-bottom:1px solid #eee">'
            f'{pic}<div style="font-size:12px;color:#999">{esc(src_name(it.source))}{extra}'
            f' · 关注：{esc(h.watch_name)}</div>'
            f'<a href="{link}" style="display:block;margin:2px 0;font-size:15px;color:#222;'
            f'text-decoration:none">{esc(it.title)}</a>'
            f'<div style="color:#e0457b;font-weight:bold">{esc(headline(h, rate))}</div>'
            f'<a href="{link}" style="display:inline-block;margin-top:8px;padding:6px 14px;font-size:14px;'
            f'color:#fff;background:#e0457b;border-radius:6px;text-decoration:none">打开商品页</a></div>')
    return "".join(rows)


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
    # short 是微信里那张消息卡片上显示的字（卡片本身放不了图，点开才有封面和链接）
    short = "；".join(f"{h.item.title[:24]} {yen(h.item.price)}" for h in hits[:3])
    resp = net.post(url, json_body={"title": summary_title(hits)[:32],
                                    "desp": markdown(hits, ctx.rate), "short": short[:64]})
    _expect(resp, "Server酱", lambda d: d.get("code") in (0, 200))


def send_pushplus(ch, hits, ctx):
    body = {"token": ch["token"], "title": summary_title(hits),
            "content": html_list(hits, ctx.rate), "template": "html"}
    if ch.get("topic"):
        body["topic"] = ch["topic"]
    resp = net.post("https://www.pushplus.plus/send", json_body=body)
    _expect(resp, "PushPlus", lambda d: d.get("code") == 200)


def send_wecom(ch, hits, ctx):
    # 图文消息一条最多 8 篇
    for i in range(0, len(hits), 8):
        articles = [{
            "title": f"【{src_name(h.item.source)}】{h.item.title}"[:120],
            "description": f"{headline(h, ctx.rate)}  关注：{h.watch_name}",
            "url": h.item.url, "picurl": cover(h.item),
        } for h in hits[i:i + 8]]
        resp = net.post(ch["webhook"], json_body={"msgtype": "news", "news": {"articles": articles}})
        _expect(resp, "企业微信", lambda d: d.get("errcode") == 0)


def send_dingtalk(ch, hits, ctx):
    url = ch["webhook"]
    if ch.get("secret"):
        ts = str(int(time.time() * 1000))
        digest = hmac.new(ch["secret"].encode(), f"{ts}\n{ch['secret']}".encode(), hashlib.sha256).digest()
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {"timestamp": ts, "sign": base64.b64encode(digest).decode()})
    title = summary_title(hits)
    resp = net.post(url, json_body={"msgtype": "markdown", "markdown": {
        "title": title, "text": f"### {title}\n\n{markdown(hits, ctx.rate)}"}})
    _expect(resp, "钉钉", lambda d: d.get("errcode") == 0)


def send_feishu(ch, hits, ctx):
    content = []
    for h in hits:
        it = h.item
        # 飞书富文本只能放上传过的图，外链图片放不进来，所以这里只有文字和链接
        content.append([{"tag": "text", "text": f"【{src_name(it.source)}】"},
                        {"tag": "a", "text": it.title[:80], "href": it.url}])
        content.append([{"tag": "text", "text": f"{headline(h, ctx.rate)}  关注：{h.watch_name}  "},
                        {"tag": "a", "text": "👉 打开商品页", "href": it.url}])
    body = {"msg_type": "post", "content": {"post": {"zh_cn": {
        "title": summary_title(hits), "content": content}}}}
    if ch.get("secret"):
        ts = str(int(time.time()))
        digest = hmac.new(f"{ts}\n{ch['secret']}".encode(), b"", hashlib.sha256).digest()
        body.update({"timestamp": ts, "sign": base64.b64encode(digest).decode()})
    resp = net.post(ch["webhook"], json_body=body)
    _expect(resp, "飞书", lambda d: d.get("code", d.get("StatusCode")) == 0)


def _per_item(hits):
    return hits[:PER_ITEM_LIMIT], hits[PER_ITEM_LIMIT:]


def send_bark(ch, hits, ctx):
    server = (ch.get("server") or "https://api.day.app").rstrip("/")
    single, rest = _per_item(hits)
    msgs = [{"title": f"{src_name(h.item.source)}｜{h.watch_name}",
             "body": f"{h.item.title}\n{headline(h, ctx.rate)}",
             "url": h.item.url, "icon": cover(h.item), "image": cover(h.item)} for h in single]
    if rest:
        msgs.append({"title": f"还有 {len(rest)} 件", "body": summary_title(rest),
                     "url": ctx.web_url or rest[0].item.url})
    for m in msgs:
        m.update({"device_key": ch["key"], "group": APP_NAME})
        resp = net.post(f"{server}/push", json_body=m)
        _expect(resp, "Bark", lambda d: d.get("code") == 200)


def send_ntfy(ch, hits, ctx):
    server = (ch.get("server") or "https://ntfy.sh").rstrip("/")
    headers = {"Authorization": f"Bearer {ch['token']}"} if ch.get("token") else None
    single, rest = _per_item(hits)
    msgs = [{"title": f"{src_name(h.item.source)}｜{h.watch_name}",
             "message": f"{h.item.title}\n{headline(h, ctx.rate)}",
             "click": h.item.url, "attach": cover(h.item) or None} for h in single]
    if rest:
        msgs.append({"title": f"还有 {len(rest)} 件", "message": summary_title(rest),
                     "click": ctx.web_url or rest[0].item.url})
    for m in msgs:
        m = {k: v for k, v in m.items() if v}
        m["topic"] = ch["topic"]
        net.post(server, json_body=m, headers=headers).check("ntfy")


def send_telegram(ch, hits, ctx):
    api = (ch.get("api") or "https://api.telegram.org").rstrip("/")
    base = f"{api}/bot{ch['token']}"
    single, rest = _per_item(hits)
    for h in single:
        it = h.item
        caption = (f"<b>{html.escape(src_name(it.source))}｜{html.escape(h.watch_name)}</b>\n"
                   f'<a href="{html.escape(it.url)}">{html.escape(it.title)}</a>\n'
                   f"{html.escape(headline(h, ctx.rate))}\n"
                   f'<a href="{html.escape(it.url)}">👉 打开商品页</a>')
        sent = False
        if cover(it):
            resp = net.post(f"{base}/sendPhoto", json_body={
                "chat_id": ch["chat_id"], "photo": cover(it), "caption": caption, "parse_mode": "HTML"})
            sent = resp.ok
        if not sent:
            resp = net.post(f"{base}/sendMessage", json_body={
                "chat_id": ch["chat_id"], "text": caption, "parse_mode": "HTML"})
            _expect(resp, "Telegram", lambda d: d.get("ok") is True)
    if rest:
        resp = net.post(f"{base}/sendMessage", json_body={
            "chat_id": ch["chat_id"], "text": f"{summary_title(rest)}\n\n{plain_lines(rest, ctx.rate)}"[:4000],
            "disable_web_page_preview": True})
        _expect(resp, "Telegram", lambda d: d.get("ok") is True)


def send_email(ch, hits, ctx):
    port = int(ch.get("port") or 465)
    user = ch["user"]
    to = [a.strip() for a in (ch.get("to") or user).replace("，", ",").split(",") if a.strip()]
    msg = MIMEText(f'<div style="font-family:sans-serif;max-width:560px">{html_list(hits, ctx.rate)}</div>',
                   "html", "utf-8")
    msg["Subject"] = Header(summary_title(hits), "utf-8")
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
    body = {"title": summary_title(hits), "text": plain_lines(hits, ctx.rate), "items": [
        {**h.item.to_dict(), "source_name": src_name(h.item.source), "watch": h.watch_name,
         "kind": h.kind, "old_price": h.old_price, "cover": cover(h.item),
         "price_cny": round(h.item.price * ctx.rate, 1) if h.item.price is not None else None}
        for h in hits]}
    net.post(ch["url"], json_body=body).check("Webhook")


SENDERS = {
    "serverchan": send_serverchan, "pushplus": send_pushplus, "wecom": send_wecom,
    "bark": send_bark, "dingtalk": send_dingtalk, "feishu": send_feishu,
    "telegram": send_telegram, "ntfy": send_ntfy, "email": send_email, "webhook": send_webhook,
}


def missing_fields(ch: dict) -> list[str]:
    spec = CHANNEL_TYPES.get(ch.get("type"))
    if not spec:
        return ["type"]
    return [label for key, label, _, required in spec[1] if required and not ch.get(key)]


def send(ch: dict, hits: list[Hit], ctx: Ctx) -> None:
    fn = SENDERS.get(ch.get("type"))
    if fn is None:
        raise ValueError(f"不认识的推送方式：{ch.get('type')}")
    missing = missing_fields(ch)
    if missing:
        raise ValueError(f"没填：{'、'.join(missing)}")
    fn(ch, hits, ctx)


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
