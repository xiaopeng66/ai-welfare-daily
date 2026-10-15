#!/usr/bin/env python3
"""正文层（2026-10-15）：给过闸的帖子抓一次正文，判出「这是哪一份 offer」。

为什么需要它：muse 30 亿 token 这类「一个官方活动被十几个人各发一条」的刷屏，
标题条条不同 —— 标题级折叠（同来源 + 同标题）一条都折不掉（实测近 48h 22 条 /
22 个标题，占线上整页 10.3%）。它们真正的身份在正文里：21 条抓到正文的帖全部
指向同一个 TG 机器人（t.me/satudigitalbot），只是各带各的 ref_ 推广号。

产物：store 行的 `offer` 字段（页面按 offer.key 折叠，×N 角标沿用现有折叠 UI）：
    "offer": {"v": 1, "key": "muse|t.me/satudigitalbot|30亿token",
              "site": "t.me/satudigitalbot", "amount": "30亿token"}
`key` 为 None = 已判过、判不出同一份 offer（比如正文里没有推广链接）——
仍写 v 标记，否则每轮都会重抓同一条。

正文缓存写**本地** `data/cache/content/`（已 gitignore）：公开仓库只留派生的
offer 字段，不落论坛正文（体积 + 转载问题）。TTL 24h。

linux.do 正文要走隐身浏览器（scrapling，CI 上没有），v1 不抓 —— 它的行不写
offer，按标题折叠，行为与今天一致。其余五个源的正文普通 HTTP 就能拿到。

纯函数部分（解析/判 offer）离线可测：tests/test_content.py 用冻结的页面片段。
"""
from __future__ import annotations

import hashlib
import html as H
import json
import os
import re
import time
from typing import Callable

OFFER_V = 1                      # offer 规则版本：改了判定就把 +1，旧行会被重算
DEFAULT_TTL_H = 24               # 正文缓存保留时长
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data", "cache", "content")
PER_SOURCE_CAP = 80              # 单轮里单一来源最多抓这么多条（防一家吃满预算）
                                 # 稳态每轮只需 5-20 条，这个数只在清积压时起作用


# ---------------------------------------------------------------- 正文解析（纯函数）
def strip_tags(s: str) -> str:
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t\u00a0]+", " ", H.unescape(s)).strip()


def _cut_between(raw: str, start_pat: str, stop_pats: list[str], limit: int = 20000) -> str:
    """截取 start_pat 之后、第一个 stop_pat 之前的内容。"""
    m = re.search(start_pat, raw, re.S)
    if not m:
        return ""
    seg = raw[m.end():m.end() + limit]
    for sp in stop_pats:
        i = re.search(sp, seg, re.S)
        if i:
            seg = seg[:i.start()]
    return strip_tags(seg)


def _slice_balanced(raw: str, start: int, tag: str = "div") -> str:
    """从 start（容器开标签之后）截到与它配对的闭合标签之前。

    为什么不用「下一个 post-footer 之类的标记」当边界：容器内部有嵌套 div，
    `</div>` 的第一个位置只是关掉内层；而「页面家具」标记（点赞/打赏/评论区）
    位置飘忽 —— 原型第一版把正文连同家具一起抽出来了（实测尾巴带着
    “展开全文 / 点赞打赏 / 登录可见”）。数配对深度才是稳的。
    """
    depth, i = 1, start
    open_re = re.compile(r"<" + tag + r"\b", re.I)
    close_re = re.compile(r"</" + tag + r"\s*>", re.I)
    while depth > 0 and i < len(raw):
        o = open_re.search(raw, i)
        c = close_re.search(raw, i)
        if not c:
            break
        if o and o.start() < c.start():
            depth += 1
            i = o.end()
        else:
            depth -= 1
            if depth == 0:
                return raw[start:c.start()]
            i = c.end()
    return raw[start:]


def _links_from(fragment: str) -> list[str]:
    """从容器 HTML 里取外链。为什么必须读 href 而不能只读纯文本：

    linux.sb 的正文里外链是「锚点文字 + href」（`<a ...>👉 TG机器人</a>`），
    strip_tags 之后只剩下「👉 TG机器人」——目标域名整段丢失（实测：`免费领3B Muse 额度`
    因此判不出 offer）。另外它的外链还套一层 `/jump?to=<urlencoded>` 跳转，
    要解码出真实目标；协议相对链接（`//t.me/x`）补 https。
    """
    out = []
    for m in re.finditer(r'href="([^"]{4,})"', fragment, re.I):
        u = H.unescape(m.group(1)).strip()
        if "?" in u and "/jump?" in u:
            from urllib.parse import unquote
            tail = u.split("to=", 1)[-1]
            u = unquote(tail.split("&", 1)[0])
        elif u.startswith("//"):
            u = "https:" + u
        if u.startswith("http://") or u.startswith("https://"):
            out.append(u)
    return out


def _body_in(raw: str, container_pat: str, tag: str = "div") -> tuple[str, list[str]]:
    m = re.search(container_pat, raw, re.S)
    if not m:
        return "", []
    inner = _slice_balanced(raw, m.end(), tag)
    return strip_tags(inner), _links_from(inner)


def body_linuxsb(raw: str, url: str) -> tuple[str, dict]:
    """linux.sb 主题页：正文在 .post-readability-content（实测 2026-10-15）。"""
    body, links = _body_in(raw, r'class="[^"]*post-readability-content[^"]*"[^>]*>')
    return body, {"links": links}


def body_baipiao(raw: str, url: str) -> tuple[str, dict]:
    """baipiao（Flarum）帖子页：楼主正文是第一个 .Post-body。"""
    body, links = _body_in(raw, r'<div[^>]+class="[^"]*Post-body[^"]*"[^>]*>')
    m = re.search(r'<h3[^>]+class="[^"]*PostUser-name[^"]*"[^>]*>([^<]{1,40})</h3>', raw)
    return body, {"author": m.group(1).strip() if m else "", "links": links}


def body_nextbuf(raw: str, url: str) -> tuple[str, dict]:
    """nextbuf 主题页：楼主正文是第一个 .markdown-body（原始 HTML 里就有，无需渲染）。"""
    body, links = _body_in(raw, r'<div[^>]+class="[^"]*markdown-body[^"]*"[^>]*>')
    body = re.sub(r"已有\s*\d+\s*人次打赏.*$", "", body, flags=re.S).strip()
    return body, {"links": links}


def body_discourse(raw: str, url: str) -> tuple[str, dict]:
    """Discourse 系（nodeloc / vibex）：/t/topic/{id}.json → post_stream.posts[0].cooked。

    linux.do 也是同构 JSON，但匿名请求会被 Cloudflare 403（管线里走 scrapling 隐身
    浏览器）——所以它的行不抓正文、不写 offer，按标题折叠，行为与今天一致。
    """
    try:
        d = json.loads(raw)
    except ValueError:
        return "", {}
    posts = (d.get("post_stream") or {}).get("posts") or []
    if not posts:
        return "", {}
    p = posts[0]
    cooked = p.get("cooked") or ""
    return strip_tags(cooked), {"author": p.get("username") or "", "links": _links_from(cooked)}


EXTRACTORS: dict[str, Callable[[str, str], tuple[str, dict]]] = {
    "linuxsb": body_linuxsb,
    "baipiao": body_baipiao,
    "nextbuf": body_nextbuf,
    "nodeloc": body_discourse,
    "vibex": body_discourse,
}


def content_url(source: str, url: str) -> str:
    """取正文要请求的地址：Discourse 系用 `.json` 端点（匿名可读、字段结构化）。"""
    if source in ("nodeloc", "vibex", "linuxdo"):
        return url.rstrip("/") + ".json"
    return url


# ---------------------------------------------------------------- offer 判定（纯函数）
BRAND = re.compile(
    r"muse|ccmax|gpt|claude|gemini|grok|deepseek|glm|qwen|kimi|mimo|river|docode|"
    r"opencode|spot|bolt|astra|dsh|comet|workbuddy|notion|codex", re.I)

# 这些域名是论坛自己/群跳转，不算「推广目标」——目标域名才是 offer 的身份。
SELF_HOSTS = ("linux.sb", "baipiao.org", "nodeloc.com", "linux.do", "vibex.iflow.cn", "nextbuf.com")
GROUP_HOSTS = ("qq.com", "weixin.qq.com", "jq.qq.com")

LINK_RE = re.compile(r"https?://([^\s/，。,）)\"'<>]+)([^\s，。,）)\"'<>]*)")
MONEY = [
    (re.compile(r"\$\s*(\d+(?:\.\d+)?)"), 1.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:美)?刀"), 1.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*美元"), 1.0),
    (re.compile(r"¥\s*(\d+(?:\.\d+)?)"), 1 / 7.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*元"), 1 / 7.0),
]
TOKENS = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*亿"), 1e8),
    (re.compile(r"(\d+(?:\.\d+)?)\s*万"), 1e4),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[Bb](?![a-zA-Z])"), 1e9),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[Mm](?![a-zA-Z])"), 1e6),
]
TOKEN_CAP = 1e11          # 1000 亿：再大基本是数字串误抽（实测 6.5e13 那种）


def _target_from_urls(urls) -> str:
    """从 explicit 链接列表里取推广目标（容器 href 比正文文字可靠，见 _links_from）。"""
    for u in urls:
        u = "https:" + u if (u or "").startswith("//") else (u or "")
        m = re.match(r"https?://([^/?#]+)([^?#]*)", u, re.I)
        if not m:
            continue
        host, path = m.group(1).lower().rstrip("."), m.group(2)
        if any(h in host for h in SELF_HOSTS) or any(h in host for h in GROUP_HOSTS):
            continue
        if host in ("t.me", "telegram.me", "telegram.dog"):
            seg = path.strip("/").split("/")[0]
            if seg:
                return f"t.me/{seg}"
            continue
        return host
    return ""


def target_of(body: str, links=()) -> str:
    """推广目标：t.me 取机器人/频道名；http 取域名。论坛自身和群链接跳过。

    先看容器里抓到的 href（可靠），没有再看正文文字里的裸 URL（Discourse cooked
    与 markdown 正文里链接常常只以文字形式出现）。
    """
    from_links = _target_from_urls(links)
    if from_links:
        return from_links
    for m in LINK_RE.finditer(body):
        host = m.group(1).lower().split("?")[0].split("#")[0].rstrip(".")
        path = m.group(2)
        if not host:
            continue
        if any(h in host for h in SELF_HOSTS) or any(h in host for h in GROUP_HOSTS):
            continue
        if host in ("t.me", "telegram.me", "telegram.dog"):
            # 身份是机器人/频道名，不是推广号：`t.me/satudigitalbot?start=ref_5312…`
            # 里 15 个人各带各的 ref —— 不切掉查询串就会把同一份拆成 15 份（实测）。
            seg = path.strip("/").split("/")[0].split("?")[0].split("#")[0]
            if seg:
                return f"t.me/{seg}"
            continue
        return host
    return ""


def amount_of(body: str) -> tuple[float, float, str]:
    """(美元额度, token 数, 展示标签)。美元与 token 取折算后更大的那个当标签。"""
    usd = 0.0
    for pat, factor in MONEY:
        for m in pat.finditer(body):
            try:
                usd = max(usd, float(m.group(1)) * factor)
            except ValueError:
                continue
    tok = 0.0
    for pat, mul in TOKENS:
        for m in pat.finditer(body):
            try:
                tok = max(tok, float(m.group(1)) * mul)
            except ValueError:
                continue
    tok = min(tok, TOKEN_CAP)
    if tok / 1e8 >= usd:                    # 1 亿 token ≈ $1 的粗折算（只用于比大小）
        label = f"{tok / 1e8:g}亿token" if tok else ""
    else:
        label = f"${usd:g}" if usd else ""
    return usd, tok, label


def offer_of(title: str, body: str, links=()) -> dict:
    """判这条帖属于哪一份 offer。判不出就返回 key=None（仍带 v 标记）。

    身份 = 品牌 + **被推广的服务**（域名 / TG 机器人名），**量值不参与**：
    实测同一个 muse 活动在语料里被写成 30亿 / 3B / 30B 三种口径，拿它当 key
    会把同一份拆成三份 —— 而「同一个机器人/同一个站」才是读者眼里的同一件事。
    量值仍照抽，放进 offer.amount 供展示与后续精选用。
    """
    site = target_of(body, links)
    _, _, amount = amount_of(body)
    m = BRAND.search(title) or BRAND.search(body[:400])
    brand = m.group(0).lower() if m else ""
    key = f"{brand}|{site}" if site else None
    return {"v": OFFER_V, "key": key, "site": site, "amount": amount}


# ---------------------------------------------------------------- 缓存
def _cache_path(url: str) -> str:
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".json")


def _cache_load(url: str, ttl_h: float) -> dict | None:
    p = _cache_path(url)
    try:
        if time.time() - os.path.getmtime(p) > ttl_h * 3600:
            return None
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("url") == url else None
    except (OSError, ValueError):
        return None


def _cache_save(url: str, body: str, meta: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = _cache_path(url) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"url": url, "body": body, "meta": meta}, f, ensure_ascii=False)
    os.replace(tmp, _cache_path(url))


# ---------------------------------------------------------------- 批量标注（网络部分很薄）
def annotate(rows: list[dict], fetch: Callable[..., str],
             limit: int = 60, delay: float = 0.4, timeout: int = 20,
             ttl_h: float = DEFAULT_TTL_H, now: float | None = None,
             log: Callable[[str], None] | None = None) -> int:
    """给缺 offer（或 offer 版本落后）的行补判。就地改 rows，返回处理条数。

    预算与礼貌：每轮最多 limit 条、单一来源最多 PER_SOURCE_CAP 条、请求间隔 delay 秒、
    失败重试一次即跳过。抓不到正文不写 offer（下一轮再试），绝不让正文层拖垮主链。
    """
    say = log or (lambda _m: None)
    todo = [
        r for r in rows
        if r.get("source", "").split("_")[0] in EXTRACTORS
        and int((r.get("offer") or {}).get("v") or 0) < OFFER_V
    ]
    if not todo or limit <= 0:
        return 0
    todo.sort(key=lambda r: r.get("created_at") or r.get("fetched_at") or "", reverse=True)
    per_src: dict[str, int] = {}
    done = 0
    for r in todo:
        if done >= limit:
            break
        src = r["source"].split("_")[0]
        if per_src.get(src, 0) >= PER_SOURCE_CAP:
            continue
        url = r.get("url") or ""
        if not url:
            continue
        cached = _cache_load(url, ttl_h)
        if cached is None:
            try:
                raw = fetch(content_url(src, url), timeout)
            except Exception:
                time.sleep(delay)
                try:
                    raw = fetch(content_url(src, url), timeout)
                except Exception:
                    say(f"[content] fetch failed: {url}")
                    continue
            body, meta = EXTRACTORS[src](raw, url)
            _cache_save(url, body, meta)
            time.sleep(delay)                    # 只有真请求才睡
        else:
            body, meta = cached.get("body", ""), cached.get("meta") or {}
        if len(body) < 30:                       # 正文过短/抽不到：不写 offer，下轮再说
            continue
        r["offer"] = offer_of(r.get("title") or "", body, meta.get("links") or ())
        if meta.get("author"):
            r["offer"]["author"] = meta["author"]
        per_src[src] = per_src.get(src, 0) + 1
        done += 1
    if done:
        say(f"[content] annotated {done} row(s) with offer keys "
            f"({len(todo) - done} still pending)")
    return done
