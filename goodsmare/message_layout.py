"""推送消息积木：配置、渠道能力和无网络的内容渲染。"""

from __future__ import annotations

import copy
import html
import re


BLOCKS = [
    ("image", "商品配图", "图片", "有图时显示，可选小图、中图或大图", "image"),
    ("title", "商品标题", "商品信息", "支持调整标题长度", "text"),
    ("status", "上新 / 降价", "商品信息", "显示这次通知的类型", "text"),
    ("item_id", "商品编号", "商品信息", "便于核对同一件商品", "text"),
    ("price", "商品价格", "价格", "按来源显示日元或人民币", "text"),
    ("cny", "人民币估算", "价格", "日元商品按当前汇率估算", "text"),
    ("drop", "降价幅度", "价格", "降价时显示原价、差额和百分比", "text"),
    ("source", "来源网站", "来源信息", "煤炉、雅虎、闲鱼等", "text"),
    ("watch", "关注名称", "来源信息", "标明是哪一个关注命中的", "text"),
    ("keyword", "搜索关键词", "来源信息", "显示关注使用的搜索词", "text"),
    ("extra", "成色 / 运费", "来源信息", "显示网站提供的补充说明", "text"),
    ("published", "上架时间", "时间", "来源提供时间时显示", "text"),
    ("found", "发现时间", "时间", "本次生成通知的时间", "text"),
    ("buy_button", "去任意门", "快捷操作", "支持的来源直接打开任意门商品页", "link"),
    ("original_button", "打开原商品页", "快捷操作", "直接查看卖家原页面", "link"),
    ("copy_original", "复制原链接", "快捷操作", "WxPusher 原生复制，其他渠道显示可复制的链接", "copy"),
    ("copy_buy", "复制任意门链接", "快捷操作", "需要转发或在另一设备打开时使用", "copy"),
    ("image_button", "查看原图", "快捷操作", "单独打开商品大图", "link"),
    ("search_button", "查看同站搜索", "快捷操作", "打开这个关注在来源网站的搜索结果", "link"),
    ("page_button", "打开推送页", "快捷操作", "已开通手机访问时显示整批商品详情", "link"),
    ("manage_button", "编辑这个关注", "快捷操作", "已开通手机访问时跳到关注编辑页", "link"),
    ("note", "自定义文字 ①", "自定义", "可插入标题、价格、关注等变量", "text"),
    ("note2", "自定义文字 ②", "自定义", "适合备注、提示或结尾文案", "text"),
    ("divider", "分隔线", "自定义", "分开内容或操作区域", "divider"),
]
KEYS = {b[0] for b in BLOCKS}
DEFAULT_LAYOUT = {
    "enabled": True,
    "blocks": ["image", "title", "price", "cny", "source", "watch", "extra", "buy_button", "copy_original", "original_button"],
    "image_size": "large", "density": "card", "title_length": 160, "max_items": 0,
    "click_target": "inherit", "title_template": "goodsmare · {数量} 件商品", "note": "", "note2": "",
}
PRESETS = {
    "shopping": {"label": "大图购物", "hint": "配图、价格、购买和复制", "settings": copy.deepcopy(DEFAULT_LAYOUT)},
    "quick": {"label": "精简抢购", "hint": "少看信息，直接去买", "settings": {
        **DEFAULT_LAYOUT, "blocks": ["title", "price", "drop", "source", "buy_button", "copy_original"],
        "density": "compact", "title_length": 70, "click_target": "doorzo"}},
    "complete": {"label": "完整信息", "hint": "来源、时间、编号和全部入口", "settings": {
        **DEFAULT_LAYOUT, "blocks": [b[0] for b in BLOCKS if b[0] not in ("note", "note2", "divider")]}},
    "text": {"label": "纯文字", "hint": "适合文字通知和低流量", "settings": {
        **DEFAULT_LAYOUT, "blocks": ["title", "price", "cny", "drop", "source", "watch", "original_button"],
        "density": "compact"}},
}
VARIABLES = ["标题", "价格", "人民币", "来源", "关注", "关键词", "状态", "数量", "原链接", "任意门链接", "商品编号"]
CAPABILITIES = {
    "wxpusher": {"image": "image", "copy": "native", "note": "正文支持大图和原生复制按钮。"},
    "bark": {"image": "image", "copy": "native", "note": "图片由系统通知展示；长按通知可复制，复制内容优先用原链接。"},
    "discord": {"image": "image", "copy": "text", "note": "标题和图片位置由 Discord 固定，其余内容按模块顺序展示。"},
    "telegram": {"image": "image", "copy": "text", "note": "支持配图和链接；长正文会使用文字消息。"},
    "ntfy": {"image": "image", "copy": "text", "note": "配图由通知附件展示；快捷入口最多显示三项，其余保留在正文。"},
    "wecom": {"image": "image", "copy": "text", "note": "自定义布局使用 Markdown 消息；旧版客户端可能只展示图片链接。"},
    "pushplus": {"image": "image", "copy": "text", "note": "图片和链接在消息详情展示。"},
    "pushme": {"image": "image", "copy": "text", "note": "支持 HTML 配图和链接；复制模块显示可长按复制的链接。"},
    "email": {"image": "image", "copy": "text", "note": "图片和操作链接显示在邮件正文。"},
    "serverchan": {"image": "image", "copy": "text", "note": "图片和链接在消息详情展示。"},
    "iyuu": {"image": "image", "copy": "text", "note": "图片和链接在消息详情展示。"},
    "dingtalk": {"image": "image", "copy": "text", "note": "图片和操作链接显示在 Markdown 正文。"},
    "feishu": {"image": "link", "copy": "text", "note": "当前接入将图片和复制模块显示为链接。"},
    "xtuis": {"image": "link", "copy": "text", "note": "当前接入将配图显示为图片链接，正文需点开查看。"},
    "pushover": {"image": "link", "copy": "text", "note": "当前接入使用文字与图片链接；正文超过 1024 字会拆分。"},
    "webhook": {"image": "link", "copy": "text", "note": "布局控制 title/text 字段，结构化商品数据仍完整提供。"},
}


def _boolean(value) -> bool:
    return str(value).strip().lower() not in ("false", "0", "off", "no", "", "none")


def normalize(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    out = copy.deepcopy(DEFAULT_LAYOUT)
    out.update({k: v for k, v in value.items() if k in out})
    out["enabled"] = _boolean(out["enabled"])
    blocks = out["blocks"] if isinstance(out["blocks"], list) else DEFAULT_LAYOUT["blocks"]
    out["blocks"] = list(dict.fromkeys(k for k in blocks if isinstance(k, str) and k in KEYS))
    for key, low, high in (("title_length", 20, 500), ("max_items", 0, 30)):
        try:
            out[key] = min(high, max(low, int(out[key])))
        except (ValueError, TypeError, OverflowError):
            out[key] = DEFAULT_LAYOUT[key]
    for key, allowed in (("image_size", ("small", "medium", "large")), ("density", ("card", "compact")),
                         ("click_target", ("inherit", "doorzo", "original", "page"))):
        if out[key] not in allowed:
            out[key] = DEFAULT_LAYOUT[key]
    for key, limit in (("title_template", 200), ("note", 500), ("note2", 500)):
        out[key] = str(out[key] or "")[:limit]
    return out


def effective(global_layout, channel: dict) -> dict | None:
    settings = normalize(channel["message"] if "message" in channel else global_layout)
    return settings if settings and settings["enabled"] else None


def enabled(settings, key: str) -> bool:
    return settings is None or key in settings["blocks"]


def _url(value) -> str:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else ""


def template(text: str, data: dict, count: int = 1) -> str:
    values = {"标题": data.get("title", ""), "价格": data.get("price", ""), "人民币": data.get("cny", ""),
              "来源": data.get("source", ""), "关注": data.get("watch", ""), "关键词": data.get("keyword", ""),
              "状态": data.get("status", ""), "数量": str(count), "原链接": data.get("original_url", ""),
              "任意门链接": data.get("buy_url", ""), "商品编号": data.get("item_id", "")}
    return re.sub(r"\{([^{}]+)\}", lambda m: str(values.get(m[1], m[0])), text)


def sections(data: dict, settings: dict) -> list[dict]:
    labels = {"price": "价格", "cny": "人民币", "drop": "降价", "source": "来源", "watch": "关注",
              "keyword": "关键词", "extra": "说明", "published": "上架", "found": "发现", "item_id": "编号"}
    links = {"buy_button": ("去任意门", "buy_url"), "original_button": ("原商品页", "original_url"),
             "copy_original": ("复制原链接", "original_url"), "copy_buy": ("复制任意门链接", "buy_url"),
             "image_button": ("查看原图", "full_image"), "search_button": ("查看同站搜索", "search_url"),
             "page_button": ("打开推送页", "page_url"), "manage_button": ("编辑这个关注", "manage_url")}
    out = []
    for key in settings["blocks"]:
        if key == "image":
            if _url(data.get("image")):
                out.append({"key": key, "kind": "image", "url": data["image"], "text": "查看配图"})
        elif key in links:
            label, field = links[key]
            if _url(data.get(field)):
                out.append({"key": key, "kind": "copy" if key.startswith("copy_") else "link",
                            "url": data[field], "text": label})
        elif key == "divider":
            out.append({"key": key, "kind": "divider", "text": "——"})
        else:
            value = template(settings[key], data)[:1000] if key in ("note", "note2") else data.get(key, "")
            if value:
                value = str(value)
                if key == "title":
                    size = settings["title_length"]
                    value = value if len(value) <= size else value[:size - 1] + "…"
                out.append({"key": key, "kind": "text", "text": f"{labels[key]}：{value}" if key in labels else value})
    return out


def render(data: dict, settings: dict, kind: str, format: str = "html", parts_only: bool = False):
    parts = sections(data, settings)
    caps = CAPABILITIES.get(kind, {"image": "link", "copy": "text"})
    compact = settings["density"] == "compact"
    out = []
    esc = html.escape
    for part in parts:
        key, typ, text, url = part["key"], part["kind"], part["text"], part.get("url", "")
        if format == "html":
            if typ == "image" and caps["image"] == "image":
                width = {"small": 96, "medium": 220, "large": 320}[settings["image_size"]]
                out.append(f'<img src="{esc(url)}" alt="商品配图" width="{width}" referrerpolicy="no-referrer" '
                           f'style="display:block;width:100%;max-width:{width}px;height:auto;border-radius:10px;margin:8px 0">')
            elif typ in ("link", "copy", "image"):
                style = ("display:inline-block;padding:9px 12px;margin:3px 7px 6px 0;border-radius:8px;"
                         "text-decoration:none;line-height:1.4;" + ("background:#d0346b;color:#fff;" if key == "buy_button"
                                                                  else "background:#edf2f8;color:#285887;"))
                if typ == "copy" and caps["copy"] == "native" and kind == "wxpusher":
                    out.append(f'<copy data-clipboard-text="{esc(url)}" style="{style}cursor:pointer">{esc(text)}</copy>')
                elif typ == "copy":
                    label = text.replace("复制", "") + "（长按复制）"
                    out.append(f'<div style="margin:6px 0;overflow-wrap:anywhere;font-size:13px">{esc(label)}：'
                               f'<a href="{esc(url)}">{esc(url)}</a></div>')
                else:
                    out.append(f'<a href="{esc(url)}" style="{style}">{esc(text)}</a>')
            elif typ == "divider":
                out.append('<hr style="border:0;border-top:1px solid #e8e8e8;margin:10px 0">')
            else:
                style = "font-size:13px;color:#666;" if key not in ("title", "price") else (
                    "font-size:16px;font-weight:650;color:#222;" if key == "title" else
                    "font-size:20px;font-weight:700;color:#d0346b;")
                out.append(f'<div style="margin:{3 if compact else 7}px 0;line-height:1.55;overflow-wrap:anywhere;'
                           f'{style}">{esc(text).replace(chr(10), "<br>")}</div>')
        elif format == "telegram":
            if typ in ("link", "copy", "image"):
                label = text.replace("复制", "") + "（长按复制）" if typ == "copy" else text
                out.append(f'<a href="{esc(url)}">{esc(label)}</a>')
            else:
                out.append(f'<b>{esc(text)}</b>' if key in ("title", "price") else esc(text))
        elif format == "markdown":
            clean = text.translate(str.maketrans({"[": "［", "]": "］", "*": "＊", "_": "＿", "`": "｀", "<": "＜", ">": "＞"}))
            safe = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            if typ == "image":
                if kind != "discord":
                    out.append(f"![]({safe})" if caps["image"] == "image" else f"[{clean}]({safe})")
            elif typ == "link":
                out.append(f"[{clean}]({safe})")
            elif typ == "copy":
                out.append(f"{clean.replace('复制', '')}（长按复制）：{url}")
            else:
                out.append(f"**{clean}**" if key in ("title", "price") else clean)
        else:
            if typ == "image" and caps["image"] == "image":
                continue  # 系统通知的配图单独附上，避免正文重复一长串图片网址。
            label = text.replace("复制", "") + "（长按复制）" if typ == "copy" else text
            out.append(f"{label}：{url}" if url else text)
    if not out:
        out = ["商品配图见消息附件。" if any(p["kind"] == "image" for p in parts) and caps["image"] == "image"
               else "这件商品没有所选模块的信息。"]
    if parts_only:
        return out
    if format == "html":
        return ('<div style="margin:0 0 16px;padding:0 0 12px;border-bottom:1px solid #e8e8e8">'
                + "".join(out) + "</div>")
    return ("\n\n" if format == "markdown" else "\n").join(out)


def warnings(settings: dict, kind: str, phone: bool = False) -> list[str]:
    caps = CAPABILITIES.get(kind, {"image": "link", "copy": "text", "note": ""})
    out = [caps["note"]] if caps.get("note") else []
    selected = set(settings["blocks"])
    if selected & {"copy_original", "copy_buy"} and caps["copy"] != "native":
        out.append("复制模块在这个接入中显示为可长按复制的链接。")
    if "image" in selected and caps["image"] == "link":
        out.append("配图在这个接入中显示为图片链接。")
    if not phone and (selected & {"page_button", "manage_button"} or settings["click_target"] == "page"):
        out.append("推送页和编辑关注入口需要先配置「设置 → 手机访问」；未配置时会隐藏，点击目标退回原商品页。")
    return out


def schema() -> dict:
    return {"blocks": [{"key": k, "label": label, "group": group, "hint": hint, "kind": kind}
                       for k, label, group, hint, kind in BLOCKS], "defaults": copy.deepcopy(DEFAULT_LAYOUT),
            "presets": copy.deepcopy(PRESETS), "variables": list(VARIABLES), "capabilities": copy.deepcopy(CAPABILITIES)}
