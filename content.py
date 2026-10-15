#!/usr/bin/env python3
"""正文层（2026-10-15）：给过闸的帖子抓一次正文，判出「这是哪一份 offer」。

为什么需要它：muse 30 亿 token 这类「一个官方活动被十几个人各发一条」的刷屏，
标题条条不同 —— 标题级折叠（同来源 + 同标题）一条都折不掉（实测近 48h 22 条 /
22 个标题，占线上整页 10.3%）。它们真正的身份在正文里：21 条抓到正文的帖全部
指向同一个 TG 机器人（t.me/satudigitalbot），只是各带各的 ref_ 推广号。

产物：store 行的 `offer` 字段（页面按 offer.key 折叠，×N 角标沿用现有折叠 UI）：
    "offer": {"v": 3, "key": "t.me/satudigitalbot", "site": "t.me/satudigitalbot",
              "amount": "30亿token", "brand": "muse"}
`key` 为 None = 已判过、判不出同一份 offer（比如正文里没有推广链接）——
仍写 v 标记，否则每轮都会重抓同一条。

正文缓存写**本地** `data/cache/content/`（已 gitignore）：公开仓库只留派生的
offer 字段，不落论坛正文（体积 + 转载问题）。TTL 24h。

v3 规则要点（每条都对应一次实测踩坑，见 tests/test_content.py 的注释）：
  · 身份 = 归一后的**被推广站点**，不含品牌、不含量值；
  · 注册域归一（去 www、子域归主域），多租户平台保留全名；
  · 短链/代码托管/网盘/商店/社交/**别家论坛**/静态资源一律不算目标；
  · linux.do 匿名 403（Cloudflare），走 scrapling 隐身浏览器，且要限速（连打会 429）。

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

OFFER_V = 6                      # offer 规则版本：改了判定就 +1，旧行下一轮自动重判
                                 # （v6：正文声明隐藏内容时只认入口形状的 URL；v5：量值单位边界 + offer 会过期重判；v4：免费二级域保留全名；v3：资源类 URL 不算目标 + linux.do 自家 CDN；
                                 #   v2：注册域归一 + 传送/平台宿主排除 + 身份只看站点）
DEFAULT_TTL_H = 24               # 正文缓存保留时长
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data", "cache", "content")
PER_SOURCE_CAP = 80              # 单轮里单一来源最多抓这么多条（防一家吃满预算）
                                 # 稳态每轮只需 5-20 条，这个数只在清积压时起作用
# 匿名 HTTP 拿不到正文的源：Cloudflare 挡着，必须走 scrapling 隐身浏览器（管线里本来
# 就为 linux.do 的列表抓取装着它）。单轮条数单独限流 —— 每条约 20 秒，不能拖垮主链。
STEALTH_SOURCES = ("linuxdo",)


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

    资源类 URL 不算链接：Discourse 的 cooked 里图片常包在 `<a href="...png">` 里，
    实测把 7 条不相干的 linux.do 帖并成了一份（key=ldstatic.com）。
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
        if not (u.startswith("http://") or u.startswith("https://")):
            continue
        if ASSET_RE.search(u):
            continue
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
    """Discourse 系（nodeloc / vibex / linux.do）：/t/topic/{id}.json → posts[0].cooked。

    linux.do 是同一端点，但匿名请求会被 Cloudflare 403 —— 要走 stealth_fetch。
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
    "linuxdo": body_discourse,      # 同构 Discourse；需要 stealth_fetch 才拿得到
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

# 这些域名是论坛自己/群跳转，不算「推广目标」——目标站点才是 offer 的身份。
SELF_HOSTS = ("linux.sb", "baipiao.org", "nodeloc.com", "linux.do", "ldstatic.com",
              "vibex.iflow.cn", "nextbuf.com")   # ldstatic.com 是 linux.do 的静态资源 CDN
GROUP_HOSTS = ("qq.com", "weixin.qq.com", "jq.qq.com")
# 「传送/平台」宿主：它们是链接的载体，不是被推广的服务 —— 拿它们当身份会误合并。
# 实测（2026-10-15 审计）：`dsh|github.com` 把两个不同项目并成一份、`muse|www.nodeseek.com`
# 把另一个论坛的帖子当成了推广目标、`muse|t.co` 是短链、`spot|apps.apple.com` 是商店页。
SKIP_HOSTS = (
    # 短链
    "t.co", "bit.ly", "s.id", "tinyurl.com", "is.gd", "dwz.date", "url.cn", "rebrand.ly",
    # 代码托管
    "github.com", "gist.github.com", "gitlab.com", "gitee.com",
    # 网盘 / 文档
    "pan.baidu.com", "drive.google.com", "docs.google.com", "dropbox.com", "mega.nz",
    "mediafire.com", "123pan.com", "aliyundrive.com", "weiyun.com", "quark.cn",
    # 商店 / 社交 / 视频
    "apps.apple.com", "play.google.com", "x.com", "twitter.com", "youtube.com",
    "youtu.be", "bilibili.com", "zhihu.com",
    # 其他论坛社区（它们不是「被推广的服务」）
    "nodeseek.com", "v2ex.com", "hostloc.com", "52pojie.cn", "tieba.baidu.com",
)
SKIP_SUFFIX = ("lanzou", "lanzoux", "lanzouw", "lanzoui", "lanzoup")   # 蓝奏云各镜像
# 多租户平台：注册域是共享的，身份必须用完整主机名（否则把不同人的部署并成一份）。
# 实测：`laisiai.tw.cpolar.io`（隧道）、`zoria.up.railway.app`（平台子域）。
MULTI_TENANT = ("github.io", "pages.dev", "vercel.app", "netlify.app", "workers.dev",
                "railway.app", "cpolar.io", "trycloudflare.com", "ngrok.io", "ngrok-free.app",
                "blogspot.com", "notion.site", "gitbook.io", "surge.sh", "replit.app",
                # 免费二级域（谁都能注册子域）：实测 bot.cd 上同时挂着 Zynk API 和另一个
                # Codex 招募站，按注册域归一就把两家并成了一份 —— 这类必须保留全名。
                "bot.cd", "cc.cd", "l.cd", "eu.org", "us.kg", "dpdns.org", "rr.nu", "zz.ac")
# 两段式公共后缀（没有 PSL 依赖，只收常见的）
MULTI_SUFFIX = ("com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "com.tw", "com.hk",
                "co.jp", "co.uk", "com.au", "co.kr", "com.sg", "com.br", "co.in")

LINK_RE = re.compile(r"https?://([^\s/，。,）)\"'<>]+)([^\s，。,）)\"'<>]*)")
ASSET_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|svg|ico|bmp|mp4|webm|mov|css|js|woff2?|ttf)(?:$|[?#])", re.I)
MONEY = [
    (re.compile(r"\$\s*(\d+(?:\.\d+)?)"), 1.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:美)?刀"), 1.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*美元"), 1.0),
    (re.compile(r"¥\s*(\d+(?:\.\d+)?)"), 1 / 7.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*元"), 1 / 7.0),
]
TOKENS = [
    # 左边界 (?<![A-Za-z0-9])：网址里的邀请码 `…/i/4JHSM73B` 曾被当成「73 billion tokens」
    # = 730 亿（2026-10-15 审计：8 行库存被这一条污染）。B/M 还要额外要求附近有 token
    # 语境，否则模型参数 `Qwen3.8 27B`、文件大小 `10MB`、限速 `10M` 都会被当成额度。
    (re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*亿"), 1e8),
    (re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*万"), 1e4),
    (re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*[Bb](?![a-zA-Z])"), 1e9),
    (re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*[Mm](?![a-zA-Z])"), 1e6),
]
TOKENS_ASCII_UNITS = {1e9, 1e6}   # B/M 需要 token 语境；亿/万 在中文语料里基本就是数量词
TOKEN_CONTEXT = re.compile(r"token|词元|额度|算力|积分|credits?", re.I)
# 「送的额度」优先：紧挨着发放动词的数才是这次要宣传的量（`领取20美元…月费99美元` 取 20）
GIVEAWAY_NEAR = re.compile(r"(?:送|赠|领|得|给|注册|免费|白嫖|薅|发|兑换|到账)\s*[^\d]{0,6}$")
MIN_TOKEN_LABEL = 1e6             # 低于 100 万 token 不生成标签：`10万人社区` 这类会被挡掉
# 价格语境的金额不是「送的额度」（`需买7元套餐`/`月费99美元`）；但 `充值送20刀` 这类
# 价格词后面跟着发放动词的不能排除 —— 判据就是「紧挨着价格词」这一条。
PRICE_CONTEXT = re.compile(r"(?:买|购买|售价|价格|仅需|包月|月费|年费|付费)\s*$")
TOKEN_CAP = 1e11          # 1000 亿：再大基本是数字串误抽（实测 6.5e13 那种）


def _host_of(url: str) -> tuple[str, str]:
    m = re.match(r"https?://([^/?#]+)([^?#]*)", url or "", re.I)
    return (m.group(1).lower().rstrip("."), m.group(2)) if m else ("", "")


def skipped_host(host: str) -> bool:
    """传送/平台宿主：不能当 offer 身份（论坛自身、群链接、短链、代码托管、网盘、商店、别家论坛）。"""
    if any(host == h or host.endswith("." + h) for h in SELF_HOSTS + GROUP_HOSTS):
        return True
    if any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
        return True
    return any(sfx in host for sfx in SKIP_SUFFIX)


def normalize_site(host: str) -> str:
    """宿主 → offer 身份的写法：去 www、同站子域归一到注册域、多租户平台保留全名。

    实测审计（2026-10-15）：
      `www.workbuddy.ai` 与 `workbuddy.ai` 曾是两把 key（同一份被拆开）；
      `river.ai` 与 `console.river.ai` 也是（同一站的主域与子域）；
      `dzhsmartai.com` 因为品牌取自标题，同时以 claude/deepseek/gpt/无 四种身份出现。
    所以：身份 = 归一后的站点，**不含品牌**（品牌仍抽出来放 offer.brand 供展示）。
    """
    host = (host or "").lower().lstrip(".").rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if any(host == h or host.endswith("." + h) for h in MULTI_TENANT):
        return host                                  # 多租户：全名才是身份
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in MULTI_SUFFIX:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# 正文声明「回复/登录可见」时，真正的登记链接是藏起来的：这时只认**入口形状**的 URL
# （根路径、/v1|/api 端点、register/sign-up/invite 等、t.me），否则会把正文里顺带提到的
# 报表/推广位链接当成身份。实测（2026-10-15 身份审计）：linux.sb/23823（Zynk）正文写着
# 「链接🔗：🔒 回复后可见」，可见外链只有 `modeloc.com/r/…` 与一份检测报告，
# 于是这条 Zynk 帖被并进了 modeloc 组（3 行）；规则改后只这一行失去身份，
# 其余 6 条「base url: …/v1」的分享帖身份保持不变。
HIDDEN_TARGET = re.compile(r"回复后可见|回复可见|登录后可见|登录可见|注册后可见|需回复|隐藏内容")
ENTRY_URL = re.compile(
    r"/(?:register|sign-?up|invite|join|portal|login|auth|v1|api|openai|chat|i)\b|t\.me/", re.I)


def _is_entry_url(u: str) -> bool:
    m = re.match(r"https?://([^/?#]+)([^?#]*)", u or "", re.I)
    if not m:
        return False
    return m.group(2) in ("", "/") or bool(ENTRY_URL.search(u))


def _target_from_urls(urls) -> str:
    """从 explicit 链接列表里取推广目标（容器 href 比正文文字可靠，见 _links_from）。"""
    for u in urls:
        u = "https:" + u if (u or "").startswith("//") else (u or "")
        if ASSET_RE.search(u):
            continue
        host, path = _host_of(u)
        if not host or skipped_host(host):
            continue
        if host in ("t.me", "telegram.me", "telegram.dog"):
            # 身份是机器人/频道名，不是推广号：`t.me/satudigitalbot?start=ref_5312…`
            # 里 15 个人各带各的 ref —— 不切掉查询串就会把同一份拆成 15 份（实测）。
            seg = path.strip("/").split("/")[0].split("?")[0].split("#")[0]
            if seg:
                return f"t.me/{seg}"
            continue
        return normalize_site(host)
    return ""


def target_of(body: str, links=()) -> str:
    """推广目标：t.me 取机器人/频道名；http 取归一后的站点名。论坛自身和群链接跳过。

    先看容器里抓到的 href（可靠），没有再看正文文字里的裸 URL（Discourse cooked
    与 markdown 正文里链接常常只以文字形式出现）。
    """
    hidden = bool(HIDDEN_TARGET.search(body))
    pool = [u for u in links if _is_entry_url(u)] if hidden else list(links)
    from_links = _target_from_urls(pool)
    if from_links:
        return from_links
    for m in LINK_RE.finditer(body):
        if hidden and not _is_entry_url(m.group(0)):
            continue
        host = m.group(1).lower().split("?")[0].split("#")[0].rstrip(".")
        path = m.group(2)
        if not host or skipped_host(host) or ASSET_RE.search(m.group(0)):
            continue
        if host in ("t.me", "telegram.me", "telegram.dog"):
            seg = path.strip("/").split("/")[0].split("?")[0].split("#")[0]
            if seg:
                return f"t.me/{seg}"
            continue
        return normalize_site(host)
    return ""


def amount_of(body: str) -> tuple[float, float, str]:
    """(美元额度, token 数, 展示标签)。

    三条约束（每条都被实测逼出来，见 tests/test_content.py 的注释）：
      · 单位左边界：不吃 URL/ID 里的数字（`…/73B`）；
      · B/M 单位要求附近有 token 语境（`3B token` ✓、`Qwen3.8 27B` ✗、`10MB` ✗）；
      · **优先取领取语境的数**（`领取20美元…月费99美元` 取 20 而不是 99），全篇都很远时
        才退回最大值（老行为，`50美元的API Key` 这类没有动词的标题还靠它）。
    """
    money: list[tuple[float, int]] = []
    for pat, factor in MONEY:
        for m in pat.finditer(body):
            try:
                value = float(m.group(1)) * factor
            except ValueError:
                continue
            if PRICE_CONTEXT.search(body[max(0, m.start() - 8):m.start()]):
                continue
            money.append((value, m.start()))
    tokens: list[tuple[float, int]] = []
    for pat, mul in TOKENS:
        for m in pat.finditer(body):
            try:
                value = float(m.group(1)) * mul
            except ValueError:
                continue
            if mul in TOKENS_ASCII_UNITS:
                window = body[max(0, m.start() - 16):m.end() + 16]
                if not TOKEN_CONTEXT.search(window):
                    continue
            tokens.append((value, m.start()))

    def pick(items: list[tuple[float, int]]) -> float:
        if not items:
            return 0.0
        near = [v for v, pos in items if GIVEAWAY_NEAR.search(body[max(0, pos - 12):pos])]
        return max(near) if near else max(v for v, _ in items)

    usd = pick(money)
    tok = min(pick(tokens), TOKEN_CAP)
    if tok >= MIN_TOKEN_LABEL and tok / 1e8 >= usd:   # 1 亿 token ≈ $1 的粗折算（只用于比大小）
        label = f"{tok / 1e8:g}亿token"
    else:
        label = f"${usd:g}" if usd else ""
    return usd, tok, label


GIVEAWAY_SENT = re.compile(r"送|赠|领|得|给|注册|免费|白嫖|薅|发|兑换|到账")
LEAD_CHARS = 300
ROUNDUP_CHARS = 600        # 超此长度按「复制来的汇总」处理：只信标题与开头


def amount_in_offer(title: str, body: str) -> str:
    """`offer.amount` 的口径：先信标题，再看正文开头；长文不再往深处取。

    实测（2026-10-15）：9 条 9k–11k 字的长文是复制来的「站点分享」汇总，正文里有几十家
    站的价格与额度 —— 全篇取最大会把它算成这条帖自己的量（先是 URL 里的 730 亿，改成
    领取语境后变成汇总里另一家的 $100）。标题是发帖人自己对这份 offer 的报价，最可靠；
    真正的单帖（正文短）才继续往回找含发放动词的句子。
    """
    for text in (title, body[:LEAD_CHARS]):
        label = amount_of(text)[2]
        if label:
            return label
    if len(body) > ROUNDUP_CHARS:
        return ""
    picked = [sent for sent in re.split(r"[。！!？?\n]", body[LEAD_CHARS:])
              if GIVEAWAY_SENT.search(sent)][:2]
    return amount_of(" ".join(picked))[2] if picked else ""


def offer_of(title: str, body: str, links=()) -> dict:
    """判这条帖属于哪一份 offer。判不出就返回 key=None（仍带 v 标记）。

    身份 = **归一后的被推广站点**（域名 / TG 机器人名）：
    · 品牌不参与 —— 实测同一站 `dzhsmartai.com` 的帖子标题分别写 claude/deepseek/gpt，
      按「品牌|站点」会被拆成四份；读者眼里的同一件事就是「同一个站」。
    · 量值不参与 —— 同一个 muse 活动在语料里被写成 30亿 / 3B / 30B 三种口径。
    品牌与量值仍照抽，放进 offer.brand / offer.amount 供展示与后续精选用。
    """
    site = target_of(body, links)
    amount = amount_in_offer(title, body)
    m = BRAND.search(title) or BRAND.search(body[:400])
    brand = m.group(0).lower() if m else ""
    return {"v": OFFER_V, "key": (site or None), "site": site, "amount": amount, "brand": brand}


# 纯函数到这里为止；`at` 时间戳由 _judge 落笔（offer_of 保持可离线断言）。


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
def _judge(row: dict, body: str, meta: dict, now: float | None = None) -> bool:
    """把**已抽好的正文**判成 offer 写回行里；正文太短（抽不到）返回 False。

    注意入参是抽好的正文，不是原始页面：_fetch_with_cache 负责「取 + 抽」，
    这里只判 —— 早期版本在这里又抽了一遍，等于对纯文本再找 HTML 容器，恒为空。
    `at` 是判定时刻：offer 也会过期（源站改推广链接、活动结束），见 _offer_stale。
    """
    if len(body) < 30:
        return False
    row["offer"] = offer_of(row.get("title") or "", body, (meta or {}).get("links") or ())
    row["offer"]["at"] = int(time.time() if now is None else now)
    if (meta or {}).get("author"):
        row["offer"]["author"] = meta["author"]
    return True


def _fetch_with_cache(row: dict, fetcher, cache_url: str, delay: float, timeout: int,
                      ttl_h: float) -> dict | None:
    """先查本地缓存，没有再真抓（失败重试一次）；抓到就写缓存。"""
    url = row.get("url") or ""
    cached = _cache_load(url, ttl_h)
    if cached is not None:
        return cached
    raw = None
    for _attempt in (1, 2):
        try:
            raw = fetcher(cache_url, timeout)
            break
        except TypeError:
            try:
                raw = fetcher(cache_url)
                break
            except Exception:
                time.sleep(delay)
        except Exception:
            time.sleep(delay)
    if raw is None:
        return None
    body, meta = EXTRACTORS[row["source"].split("_")[0]](raw, url)
    _cache_save(url, body, meta)
    time.sleep(delay)                  # 只有真请求才睡
    return {"body": body, "meta": meta}


def _offer_stale(row: dict, ttl_h: float, now: float) -> bool:
    """这一行的 offer 该不该（重新）判？三种情况：

      · 从没判过、或规则版本落后（OFFER_V 升级）；
      · 判过但**超过 TTL** —— 缓存会过期，offer 不能比缓存活得久（源站会改推广链接、
        活动会结束，2026-10-15 审计：正值与 null 都不会重判）；
      · 没有 `at` 时间戳的旧数据（当陈旧，重判一次把时间戳补上）。
    """
    o = row.get("offer") or {}
    if int(o.get("v") or 0) < OFFER_V:
        return True
    at = o.get("at")
    if not at:
        return True
    try:
        return (now - float(at)) > ttl_h * 3600
    except (TypeError, ValueError):
        return True


def annotate(rows: list[dict], fetch: Callable[..., str],
             limit: int = 60, delay: float = 0.4, timeout: int = 20,
             ttl_h: float = DEFAULT_TTL_H, now: float | None = None,
             stealth_fetch: Callable[..., str] | None = None, stealth_limit: int = 8,
             stealth_delay: float = 15.0,
             log: Callable[[str], None] | None = None) -> int:
    """给缺 offer（过期/版本落后也算）的行补判。就地改 rows，返回判定成功条数。

    预算按**尝试**计数（2026-10-15 审计：原来按成功数计数，一轮失败风暴会把请求放大到
    限额的数倍 —— limit=1 时 4 条失败候选产生了 8 次抓取调用，单源上限也被绕过）：
      · 普通通道：每轮最多尝试 limit 条、单一来源最多尝试 PER_SOURCE_CAP 条、
        请求间隔 delay、失败重试一次即跳过；
      · 隐身通道：每轮最多尝试 stealth_limit 条、每条之间 ≥ stealth_delay 秒、
        **「200 但正文不可用」也算失败**（原来不算，熔断形同虚设）、连续两条失败即收手。
    抓不到正文不写 offer（下一轮再试），绝不让正文层拖垮主链。
    """
    say = log or (lambda _m: None)
    clock = time.time() if now is None else now
    todo = [
        r for r in rows
        if r.get("source", "").split("_")[0] in EXTRACTORS and _offer_stale(r, ttl_h, clock)
    ]
    if not todo or (limit <= 0 and not stealth_fetch):
        return 0

    # 先判没判过的（新帖优先），再按创建时间新的先重判 —— 不这样排，重判会把预算吃光。
    def _order(r: dict):
        o = r.get("offer") or {}
        first_time = int(o.get("v") or 0) < OFFER_V or not o.get("at")
        return (0 if first_time else 1, r.get("created_at") or r.get("fetched_at") or "")

    todo.sort(key=_order)

    normal = [r for r in todo if r["source"].split("_")[0] not in STEALTH_SOURCES]
    stealth = [r for r in todo if r["source"].split("_")[0] in STEALTH_SOURCES]
    attempted = 0
    done = 0

    per_src: dict[str, int] = {}
    for r in normal:
        if attempted >= limit:
            break
        src = r["source"].split("_")[0]
        if per_src.get(src, 0) >= PER_SOURCE_CAP or not r.get("url"):
            continue
        attempted += 1                       # 尝试即计数：失败也占预算
        per_src[src] = per_src.get(src, 0) + 1
        got = _fetch_with_cache(r, fetch, content_url(src, r["url"]), delay, timeout, ttl_h)
        if got is None:
            continue
        if _judge(r, got["body"], got["meta"], clock):
            done += 1

    stealth_done = 0
    stealth_attempted = 0
    if stealth_fetch:
        # linux.do 会限速：实测连着打（每 2-3 秒一条）立刻连续 429，之后连正常请求也一并 429。
        # 所以这里要么慢、要么停 —— 连续两条失败就放弃本轮（下一轮再来），绝不硬打。
        fails = 0
        for r in stealth:
            if stealth_attempted >= stealth_limit or fails >= 2:
                break
            if not r.get("url"):
                continue
            stealth_attempted += 1           # 尝试即计数
            got = _fetch_with_cache(r, stealth_fetch, content_url("linuxdo", r["url"]),
                                    max(delay, stealth_delay), timeout, ttl_h)
            if got is None or not _judge(r, got["body"], got["meta"], clock):
                fails += 1                   # 「200 但正文不可用」也计入熔断
                time.sleep(max(delay, stealth_delay) * 2)
                continue
            fails = 0
            stealth_done += 1
            done += 1

    if done or attempted or stealth_attempted:
        remaining = sum(1 for r in todo if _offer_stale(r, ttl_h, clock))
        say(f"[content] annotated {done} row(s) with offer keys "
            f"({remaining} still pending; {stealth_done} via stealth browser; "
            f"tried {attempted + stealth_attempted})")
    return done
