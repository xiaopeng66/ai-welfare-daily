#!/usr/bin/env python3
"""正文层（content.py）的离线测试：五个源的正文抽取 + offer 判定 + annotate 预算/缓存。

为什么必须有这些夹具：2026-10-15 muse 30 亿被十几个人各发一条（近 48h 22 条 / 22 个
标题，占线上整页 10.3%），标题级折叠一条都折不掉 —— 判定「是不是同一份 offer」只能
看正文。这里的样例页来自当天真实抓取（tests/evidence/content-20261015/），
offers 的判定必须钉死在"同一份合并、不同份绝不合"两侧。

跑：python3 tests/test_content.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import content as c  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence", "content-20261015")
RESULTS = []


def check(ok, msg, extra=None):
    print(("ok    " if ok else "FAIL  ") + msg + ("" if ok else f"   ← {extra}"))
    RESULTS.append(bool(ok))


def fixture(name: str) -> str:
    return open(os.path.join(FX, name), encoding="utf-8").read()


# 页面家具：正文抽取把「点赞打赏/登录可见/展开全文」带进来就是回归（原型第一版踩过）。
FURNITURE = ("展开全文", "点赞打赏", "登录可见", "发表回复", "注册登录", "人次打赏", "Processed in")

print("== 正文抽取：五个源各一个真实样例 ==")
BODY_CASES = [
    ("linuxsb", "linuxsb-topic-25907.html", "jojatoken.com", 80),
    ("baipiao", "baipiao-topic-766.html", "console.river.ai", 120),
    ("nextbuf", "nextbuf-topic-342.html", "monetwl.com", 30),
    ("nodeloc", "nodeloc-topic-113917.json", "NexoToken", 1500),
    ("vibex", "vibex-topic-6716.json", "workbuddy", 150),
]
for src, name, needle, min_len in BODY_CASES:
    raw = fixture(name)
    body, meta = c.EXTRACTORS[src](raw, "https://example.invalid")
    check(len(body) >= min_len, f"{src}: 正文抽出来了（{len(body)} 字 ≥ {min_len}）", body[:80])
    check(needle in body, f"{src}: 正文含关键内容 {needle!r}")
    bad = [w for w in FURNITURE if w in body]
    check(not bad, f"{src}: 正文不含页面家具", bad)

print("\n== v2 规则：站点归一 / 传送宿主排除 / 身份不含品牌 ==")
# 审计（2026-10-15 真实 store）发现的误拆分与误合并，逐条钉死：
check(c.offer_of("a", "https://www.workbuddy.ai/x")["key"]
      == c.offer_of("b", "https://workbuddy.ai/y")["key"] == "workbuddy.ai",
      "www 前缀归一（实测 www.workbuddy.ai 与 workbuddy.ai 曾是两把 key）")
check(c.offer_of("a", "https://console.river.ai/")["key"]
      == c.offer_of("b", "https://river.ai/")["key"] == "river.ai",
      "同站子域归一到注册域（console.river.ai 与 river.ai）")
check(c.offer_of("claude 免费领", "https://dzhsmartai.com/a")["key"]
      == c.offer_of("deepseek 送额度", "https://dzhsmartai.com/b")["key"],
      "身份不含品牌：同一站标题分别写 claude/deepseek 也合并（实测被拆成四份）")
check(c.normalize_site("developer.amd.com.cn") == "amd.com.cn"
      and c.normalize_site("free.zynk.bot.cd") == "bot.cd", "两段式后缀（com.cn / *.cd）按注册域取")
for u, host in [("https://github.com/x/y", "github.com"), ("https://t.co/abc", "t.co"),
                ("https://www.nodeseek.com/post-1", "nodeseek.com"),
                ("https://wwbpt.lanzouw.com/xyz", "蓝奏云"), ("https://apps.apple.com/cn/app/x", "App Store"),
                ("https://drive.google.com/file/d/1", "Google Drive")]:
    check(c.offer_of("某帖", u)["key"] is None, f"{host} 是传送/平台宿主，不构成 offer 身份")
check(c.offer_of("a", "https://a.github.io/x")["key"] != c.offer_of("b", "https://b.github.io/y")["key"],
      "多租户平台保留完整主机名：两个 pages 部署不合并")
check(c.normalize_site("laisiai.tw.cpolar.io") == "laisiai.tw.cpolar.io",
      "隧道域名（cpolar.io）不算可归一的注册域")
check(c.offer_of("muse 30亿", "https://t.co/abc 短链 https://satudigital.bot.cd/join 真目标")["site"] == "bot.cd",
      "短链跳过、取后面的真实目标")

print("\n== v3 规则：静态资源与自身 CDN 不算目标 ==")
# 实测（2026-10-15）：Discourse 的 cooked 里图片常包在 <a href="...png"> 里，
# ldstatic.com（linux.do 的静态资源 CDN）因此成了 7 条不相干帖子的共同身份。
check(c.skipped_host("ldstatic.com") and c.skipped_host("cdn.ldstatic.com"),
      "linux.do 的静态资源 CDN（ldstatic.com）计入自身域名")
check(c.offer_of("某帖", "看这张图 https://img.example.com/a.png")["site"] == "",
      "纯文本里的图片链接不算推广目标")
check(c.offer_of("某帖", "图 https://img.example.com/a.png 真站 https://station-z.com/reg")["site"]
      == "station-z.com", "跳过图片后取后面的真目标")
check(c._links_from('<a href="https://ldstatic.com/x.png"><img src="x"></a>') == [],
      "容器 href 里的资源 URL 一律丢弃（cooked 的图片包裹链接）")
check(c._links_from('<a href="https://t.me/somebot?start=1">进群</a>') == ["https://t.me/somebot?start=1"],
      "正常链接照旧保留")

print("\n== linux.do：隐身通道（v2 新增）==")
LD = '{"post_stream":{"posts":[{"username":"alice","cooked":"<p>https://station-y.com/reg 送 20 刀</p>"}]}}'
def boom(*a, **k):
    raise OSError("匿名 403")
ld_rows = [{"id": "9", "source": "linuxdo_welfare", "url": "https://linux.do/t/topic/9",
            "title": "linux.do 的帖", "created_at": "2026-10-15T09:00:00+00:00"}]
n_ld = c.annotate([dict(ld_rows[0])], boom, limit=0, stealth_fetch=lambda url, timeout=20: LD,
                  stealth_limit=1, delay=0, timeout=1, log=lambda _m: None)
probe = dict(ld_rows[0]); probe.pop("offer", None)
c.annotate([probe], boom, limit=0, stealth_fetch=lambda url, timeout=20: LD,
           stealth_limit=1, delay=0, timeout=1, log=lambda _m: None)
check(n_ld == 1 and probe.get("offer", {}).get("site") == "station-y.com",
      "linux.do 经 stealth_fetch 判出身份（site=station-y.com）", probe.get("offer"))
check(probe.get("offer", {}).get("key") == "station-y.com", "linux.do 的 offer.key 就是站点", probe.get("offer"))
check(c.content_url("linuxdo", "https://linux.do/t/topic/9") == "https://linux.do/t/topic/9.json",
      "linux.do 走 /t/topic/{id}.json 端点")
probe2 = dict(ld_rows[0]); probe2.pop("offer", None)
n_no = c.annotate([probe2], boom, limit=10, delay=0, timeout=1, log=lambda _m: None)
check(n_no == 0 and "offer" not in probe2,
      "没传 stealth_fetch 时 linux.do 不抓、不写（CI/无 scrapling 环境行为同 v1）")

print("\n== 链接提取：锚点文字 + /jump?to= 跳转（linux.sb 的真实外链形态）==")
# 实测：linux.sb 正文里外链是「锚点文字 + href」，strip_tags 后只剩「👉 TG机器人」；
# 而 href 又套了一层 /jump?to=<urlencoded> —— 只读纯文本会判不出任何 offer。
raw_lsb = fixture("linuxsb-topic-25907.html")
_body, _meta = c.body_linuxsb(raw_lsb, "https://linux.sb/topic/25907")
check(any("jojatoken.com" in u for u in _meta.get("links") or []),
      "linuxsb 夹具：/jump?to= 解码出真实外链（jojatoken.com）", _meta.get("links"))
check(c.offer_of("抽奖 周末福利", _body, _meta["links"])["site"] == "jojatoken.com",
      "判出的推广目标是 jojatoken.com 而不是 linux.sb")
frag = '<p>👉 <a href="/jump?to=https%3A%2F%2Ft.me%2Fsatudigitalbot%3Fstart%3D1">TG机器人</a></p>'
raw2 = '<div class="post-readability-content">' + frag + "</div>"
_b2, _m2 = c.body_linuxsb(raw2, "")
check(_m2.get("links") == ["https://t.me/satudigitalbot?start=1"],
      "锚点文字链接取到真实 URL 并解码查询串", _m2.get("links"))
check(c.offer_of("免费领3B Muse 额度", _b2, _m2["links"])["site"] == "t.me/satudigitalbot",
      "锚点文字形式的链接也能定出 TG 机器人（纯文本路径会漏）")
check(c.target_of("", ["//t.me/somebot?start=1"]) == "t.me/somebot",
      "协议相对链接（//t.me/...）补 https 后仍能定目标")

print("\n== Discourse JSON 的 .json 端点映射 ==")
check(c.content_url("nodeloc", "https://www.nodeloc.com/t/topic/113917")
      == "https://www.nodeloc.com/t/topic/113917.json", "nodeloc 走 .json 端点")
check(c.content_url("vibex", "https://vibex.iflow.cn/t/topic/6716") .endswith("6716.json"),
      "vibex 走 .json 端点")
check(c.content_url("linuxsb", "https://linux.sb/topic/25907").endswith("/topic/25907"),
      "linuxsb 直接用主题页")

print("\n== offer 判定：同一份必须合并 ==")
# muse 刷屏的真实形状：标题各不相同、正文都指向同一个 TG 机器人。
muse_bodies = [
    "https://t.me/satudigitalbot?start=ref_5312014808 增加您的 Muse 配额，每个账户最高可达 30B。",
    "免费领 30 亿 muse token  https://t.me/satudigitalbot?start=ref_6242888658 然后把邀请码发给机器人",
    "tg bot 把 muse 的价格打下来了，免费领 30 亿 token https://t.me/satudigitalbot?start=ref_8795121393",
]
keys = {c.offer_of(t, b)["key"] for t, b in zip(
    ["速领30亿token，机器人灌满muse，耍疯喽", "白嫖30亿muse token", "tg bot 把 muse 的价格打下来了"],
    muse_bodies)}
check(len(keys) == 1 and None not in keys, "muse 三条不同标题 → 同一把 offer key", keys)
check(keys.pop() == "t.me/satudigitalbot",
      "key = TG 机器人名（v2：身份只看站点，品牌进 offer.brand；ref 推广号不进 key）")

print("\n== offer 判定：不同份绝不能合并 ==")
gpt_a = c.offer_of("注册送20刀 0.15倍率gpt", "https://www.primerelay.online/register?aff=X 注册进群领10刀")
gpt_b = c.offer_of("注册送10刀 0.15倍率gpt", "https://code.wahh.cloud/register 注册就送")
gpt_c = c.offer_of("注册送gpt2亿", "https://api.moremark.app/register 送2亿token")
check(len({gpt_a["key"], gpt_b["key"], gpt_c["key"]}) == 3,
      "三家不同中转站 → 三把 key（按域名区分，不按品牌）",
      [gpt_a["key"], gpt_b["key"], gpt_c["key"]])
muse_10 = c.offer_of("Muse兑换 10亿额度", muse_bodies[0].replace("30B", "10B").replace("30 亿", "10 亿"))
check(muse_10["key"] == c.offer_of("白嫖30亿muse token", muse_bodies[1])["key"],
      "同一个机器人、量值写法不同（10亿/30亿/3B）→ 同一把 key（量值不参与身份）")
check(muse_10["amount"] != c.offer_of("白嫖30亿muse token", muse_bodies[1])["amount"],
      "但量值仍被抽出来放进 offer.amount（展示/精选要用）",
      (muse_10["amount"], c.offer_of("白嫖30亿muse token", muse_bodies[1])["amount"]))
check(c.offer_of("分享一个羊毛", "正文里没有任何链接，只有一句描述")["key"] is None,
      "没有推广链接 → key 为 None（不参与折叠）")
check(c.offer_of("某站", "https://linux.sb/topic/29999 论坛自己的链接")["key"] is None,
      "论坛自身链接不算推广目标")
check(c.offer_of("某站", "https://qm.qq.com/q/abc 群链接 https://t.me/somebot?start=1")["site"]
      == "t.me/somebot", "QQ 群链接跳过、TG 机器人当目标")

print("\n== 量值抽取的上下界 ==")
_, tok, label = c.amount_of("送 30 亿 token")
check(tok == 3e9 and label == "30亿token", "亿级 token 取值正确", label)
_, _, label2 = c.amount_of("送 $25 额度，另有 6.5e13 的数字串")
check(c.amount_of("一串乱数字 " + "9" * 20)[1] <= c.TOKEN_CAP, "token 量有上限（防数字串误抽）")
check(label2.startswith("$"), "美元与 token 比大小后取大的做标签", label2)

print("\n== 落盘：offer 必须活着穿过 store 写盘（字段白名单）==")
# 2026-10-15 实测踩坑：annotate 报「annotated 25 row(s)」，store 里却 0 行带 offer ——
# 写盘是按字段白名单重建 dict，新字段没进白名单就被静默丢掉。这里钉死它。
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("fetch_for_row", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fetch.py"))
fm = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(fm)
_row = {"id": "1", "title": "t", "url": "u", "tags": ["额度"], "score": 1, "source": "linuxsb_首页",
        "created_at": "2026-10-15T00:00:00+00:00", "published_verified": True,
        "offer": {"v": 1, "key": "muse|t.me/satudigitalbot", "site": "t.me/satudigitalbot", "amount": "30亿token"}}
out_row = fm.store_row(_row, "2026-10-15T01:00:00+00:00")
check(out_row.get("offer", {}).get("key") == "muse|t.me/satudigitalbot",
      "store_row 保留 offer（字段白名单里有它）", out_row.get("offer"))
_row2 = dict(_row); _row2.pop("offer")
check("offer" not in fm.store_row(_row2, "2026-10-15T01:00:00+00:00"),
      "没有 offer 的行不写这个键（保持逐字节不变）")
check(fm._row_changed({"offer": None}, {"offer": {"v": 1}}),
      "offer 的出现算「行变了」（fetched_at 要更新、要有提交）")
# 列表重抓不能把已判的 offer 弄丢（2026-10-15 实测：v2 行 143 → 85）
_prev = {"created_at": "2026-10-15T00:00:00+00:00", "published_verified": True,
         "offer": {"v": 2, "key": "t.me/satudigitalbot", "site": "t.me/satudigitalbot"}}
_fresh = {"id": "1", "title": "t", "url": "u", "source": "linuxsb_首页"}   # 列表行：没有 offer/created_at
fm.carry_forward(_prev, _fresh)
check(_fresh.get("offer", {}).get("key") == "t.me/satudigitalbot",
      "carry_forward：重抓的行继承上一轮的 offer（否则每轮重抓都白判）", _fresh.get("offer"))
check(_fresh.get("created_at") == "2026-10-15T00:00:00+00:00" and _fresh.get("published_verified") is True,
      "carry_forward：created_at / published_verified 照旧继承")
_own = {"offer": {"v": 2, "key": "new.example"}}
fm.carry_forward(_prev, _own)
check(_own["offer"]["key"] == "new.example", "carry_forward：新行自己判出的 offer 不被旧值覆盖")

print("\n== annotate：预算、缓存、失败跳过 ==")
tmp = tempfile.mkdtemp()
old_cache = c.CACHE_DIR
c.CACHE_DIR = tmp
try:
    rows = [
        {"id": "1", "source": "linuxsb_首页", "url": "https://linux.sb/topic/1",
         "title": "白嫖30亿muse token", "created_at": "2026-10-15T08:00:00+00:00"},
        {"id": "2", "source": "linuxsb_首页", "url": "https://linux.sb/topic/2",
         "title": "免费Muse 30亿token", "created_at": "2026-10-15T07:00:00+00:00"},
        {"id": "3", "source": "nextbuf_首页", "url": "https://www.nextbuf.com/t/3",
         "title": "别家鸡蛋", "created_at": "2026-10-15T06:00:00+00:00"},
        {"id": "4", "source": "linuxdo_welfare", "url": "https://linux.do/t/topic/4",
         "title": "linux.do 的帖（v1 不抓正文）", "created_at": "2026-10-15T05:00:00+00:00"},
    ]
    calls = []

    def fake_fetch(url, timeout=20):
        calls.append(url)
        if url.endswith("/topic/1"):
            return '<div class="post-readability-content">' + muse_bodies[0] + "</div>"
        if url.endswith("/t/3"):
            return '<div class="markdown-body"><p>http://station-x.com/reg 送 5 刀</p></div>'
        if url.endswith("/topic/5"):
            return ('<div class="post-readability-content">送 30 亿 muse token，'
                    '点此 https://t.me/satudigitalbot?start=555 进机器人领</div>')
        raise OSError("boom")            # 2 号帖：两次都失败 → 跳过、不写 offer

    n = c.annotate(rows, fake_fetch, limit=10, delay=0, timeout=1, log=lambda _m: None)
    check(n == 2, "处理了两条（1 号成功、3 号成功、2 号失败被跳过）", n)
    check(rows[0].get("offer", {}).get("key") == "t.me/satudigitalbot",
          "1 号帖写入 offer key", rows[0].get("offer"))
    check("offer" not in rows[1], "抓失败的 2 号帖不写 offer（下轮再试）")
    check("offer" not in rows[3], "linux.do 不在 EXTRACTORS 里 → 不抓、不写（行为同今天）")
    check(rows[2].get("offer", {}).get("site") == "station-x.com", "nextbuf 的 offer 判定生效")

    calls.clear()
    n2 = c.annotate(rows, fake_fetch, limit=10, delay=0, timeout=1, log=lambda _m: None)
    check(n2 == 0, "第二次 annotate 不再改动任何行", n2)
    check(set(calls) == {"https://linux.sb/topic/2"},
          "只重试了当时抓失败的那条（两次尝试），命中缓存的 1/3 号帖零请求", calls)

    rows5 = [{"id": "5", "source": "linuxsb_首页", "url": "https://linux.sb/topic/5",
              "title": "新帖", "created_at": "2026-10-15T09:00:00+00:00"}]
    c.annotate(rows5, fake_fetch, limit=1, delay=0, timeout=1, log=lambda _m: None)
    check("offer" in rows5[0], "预算不挡新行（limit=1 时该行仍被处理）")
finally:
    c.CACHE_DIR = old_cache
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
sys.exit(0 if all(RESULTS) else 1)
