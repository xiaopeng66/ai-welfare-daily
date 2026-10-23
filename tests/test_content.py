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

# 自己的 stdio 也钉成 UTF-8：断言文案/失败详情里会出现非 GBK 字符（✕ 🔥 🉐 等），

# 一旦 stdout 被重定向而 PYTHONIOENCODING 没设（手工 `python tests/xxx.py > log`），

# print 会当场 UnicodeEncodeError —— 与内容正确性无关的崩溃，还可能专挑打印失败详情

# 时炸，把真正的失败盖掉。reconfigure 不可用（stdout 被换成 StringIO 等）就跳过。

for _stream in (sys.stdout, sys.stderr):

    try:

        _stream.reconfigure(encoding="utf-8", errors="replace")

    except Exception:

        pass





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
check(c.normalize_site("developer.amd.com.cn") == "amd.com.cn",
      "两段式后缀（com.cn）按注册域取；*.cd 这类免费二级域见 v4 一节（保留全名）")
for u, host in [("https://github.com/x/y", "github.com"), ("https://t.co/abc", "t.co"),
                ("https://www.nodeseek.com/post-1", "nodeseek.com"),
                ("https://wwbpt.lanzouw.com/xyz", "蓝奏云"), ("https://apps.apple.com/cn/app/x", "App Store"),
                ("https://drive.google.com/file/d/1", "Google Drive")]:
    check(c.offer_of("某帖", u)["key"] is None, f"{host} 是传送/平台宿主，不构成 offer 身份")
check(c.offer_of("a", "https://a.github.io/x")["key"] != c.offer_of("b", "https://b.github.io/y")["key"],
      "多租户平台保留完整主机名：两个 pages 部署不合并")
check(c.normalize_site("laisiai.tw.cpolar.io") == "laisiai.tw.cpolar.io",
      "隧道域名（cpolar.io）不算可归一的注册域")
check(c.offer_of("muse 30亿", "https://t.co/abc 短链 https://satudigital.bot.cd/join 真目标")["site"]
      == "satudigital.bot.cd", "短链跳过、取后面的真实目标（v4：免费二级域保留全名）")

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

print("\n== v6 规则：正文声明「回复可见」时只认入口形状的 URL ==")
# 实测（身份审计）：linux.sb/23823（Zynk）正文写「链接🔗：🔒 回复后可见」，可见外链只有
# `modeloc.com/r/…` 与一份检测报告 → 被并进了 modeloc 组（3 行）。真链接被藏起来时，
# 只认「入口形状」的 URL（根路径 /v1|/api / register|sign-up|invite / t.me），
# 报表、推广位这类顺带提到的链接不做身份。
_zynk = c.offer_of("【Zynk API】平价优惠站点免费发福利",
                   "链接🔗： 🔒 回复后可见 回复本主题后即可查看这部分内容。 倍率 "
                   "https://modeloc.com/r/abc123 检测报告 https://mozhenzhen.com/reports/MZ1?from=verify", [])
check(_zynk["key"] is None, "隐藏正文 + 只有报表/推广位链接 → 不判身份（不再误并 modeloc 组）", _zynk)
check(c.target_of("api地址 https://sub.lunarfox.cn/v1  key 🔒 回复后可见", []) == "lunarfox.cn",
      "对照：/v1 端点是入口形状，仍是身份（6 条 key 分享帖靠它）", None)
check(c.target_of("base url: https://laisiai.tw.cpolar.io/v1 key: 回复后可见", []) == "laisiai.tw.cpolar.io",
      "对照：同站不同子域的 /v1 各自成身份（cpolar 多租户保留全名）", None)
check(c.target_of("注册 https://a.example.com/register?aff=x 送10刀", []) == "example.com",
      "对照：普通登记链接照旧", None)

print("\n== v4 规则：免费二级域保留全名 + 自身域按边界判断 ==")
# 审计（2026-10-15 第二轮）：`bot.cd` 上同时挂着 Zynk API 与另一个 Codex 招募站，
# 按注册域归一就把两家并成一份；而自身域用子串判断则会把 mylinux.sbsite.com 当论坛自身。
check(c.normalize_site("zynk.bot.cd") == "zynk.bot.cd"
      and c.normalize_site("codex.bot.cd") == "codex.bot.cd",
      "免费二级域（*.bot.cd）保留完整主机名，不同服务不合并")
check(c.normalize_site("console.river.ai") == "river.ai",
      "普通域名仍归一到注册域（对照组）")
check(not c.skipped_host("mylinux.sbsite.com") and not c.skipped_host("notlinux.do.example.com"),
      "自身域按域名边界判断：只是「含」linux.sb 的域名不算论坛自身")
check(c.skipped_host("linux.sb") and c.skipped_host("www.linux.sb") and c.skipped_host("api.linux.do"),
      "论坛自身的各子域照旧跳过")

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
# 2026-10-24 口径更新（用户点名 zcode/hyxapi 两个案例）：别名命中的活动，
# key = 活动名 —— 链接帖（t.me 机器人）与只在标题里写活动名的转帖必须同一个键。
# 防误合的边界是**人工维护的别名表**（加条目要实测证据），不是"一律不许跨入口"。
check(keys.pop() == "muse",
      "key = 活动名（别名命中；品牌进 offer.brand，ref 推广号不进 key）")

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

print("\n== 活动级身份（2026-10-16）：一个活动多个入口 ==")
# 实测：muse 一个活动在 store 里同时是 muse.ai（官网邀请码）、t.me/satudigitalbot（电报
# 机器人）、以及别人的转帖（正文没有链接，标题只写「Muse 兑换 10亿额度」）。精选要
# 「一个活动一张卡」，站点级 key 合不掉这三种入口 —— 所以加 campaign 这一层。
muse_site = c.offer_of("muse AI免费领鸡蛋邀请码",
                       "加入后的 48 小时内兑现我的邀请码，我们就能分别获得 10 亿个词元 https://muse.ai/join")
muse_bot = c.offer_of("鸡蛋！加入频道能领30亿的token",
                      "电报加入频道可以领30亿muse的token https://t.me/satudigitalbot?start=ref_1")
muse_repost = c.offer_of("Muse兑换 10亿额度", "自己把邀请码扔给机器人,上面有教程 99积分撬动10亿额度")
check(muse_site["campaign"] == muse_bot["campaign"] == muse_repost["campaign"] == "muse",
      "官网 / 电报机器人 / 无链接转帖 → 同一个活动身份",
      [muse_site["campaign"], muse_bot["campaign"], muse_repost["campaign"]])
# 2026-10-24 口径更新：三个入口 → 同一个 key（此前站点级 key 一分为三，页面三张卡）。
# 防误合的边界 = 别名表人工维护（RULES.md v9），站点字段 offer.site 仍如实保留。
check(muse_site["key"] == muse_bot["key"] == muse_repost["key"] == "muse",
      "官网 / 电报机器人 / 无链接转帖 → 同一个 key（一个活动一张卡）",
      [muse_site["key"], muse_bot["key"], muse_repost["key"]])
check(c.offer_of("注册送20刀 gpt", "https://a-relay.example/register 送20刀")["campaign"] == "a-relay.example"
      and c.offer_of("注册送10刀 gpt", "https://b-relay.example/register 送10刀")["campaign"] == "b-relay.example",
      "别名表外的一律退回站点身份（两家 gpt 中转站不会被并成一个活动）")
check(c.offer_of("羊毛分享", "正文只在结尾提了一句 muse，没有链接")["campaign"] == "",
      "站点为空时只看标题：正文顺带提到的品牌不算这个活动")

print("\n== 量值抽取的上下界 ==")
_, tok, label = c.amount_of("送 30 亿 token")
check(tok == 3e9 and label == "30亿token", "亿级 token 取值正确", label)
_, _, label2 = c.amount_of("送 $25 额度，另有 6.5e13 的数字串")
check(c.amount_of("一串乱数字 " + "9" * 20)[1] <= c.TOKEN_CAP, "token 量有上限（防数字串误抽）")
check(label2.startswith("$"), "美元与 token 比大小后取大的做标签", label2)

print("\n== 量值：单位边界 / 领取语境 / 价格语境（2026-10-15 审计）==")
# 8 行库存曾被 `…/i/4JHSM73B` 污染成「730 亿 token」；模型参数、文件大小、限速也都被
# 当成过额度。左边界 + B/M 需要 token 语境 + 优先取领取语境，三条一起解决。
for text, want_tok, why in [
        ("注册领3B token", 3e9, "3B token 是真额度"),
        ("MUSE token 免费3B TG注册机", 3e9, "token 在附近也算语境"),
        ("注册链接 https://modeloc.com/i/EXAMPLE73B", 0.0, "URL 邀请码里的 73B 不是额度"),
        ("可用模型 Qwen3.8 27B", 0.0, "27B 是模型参数规模"),
        ("下载文件 10MB，版本2.0", 0.0, "10MB 是文件大小"),
        ("模型延迟 10ms", 0.0, "10ms 是延迟"),
        ("每分钟10M吞吐", 0.0, "10M 是吞吐指标"),
        ("欢迎加入10万人社区", None, "人数不生成 token 标签")]:
    tok = c.amount_of(text)[1]
    if want_tok is None:
        check(c.amount_of(text)[2] == "", f"量值标签为空（{why}）", c.amount_of(text))
    else:
        check(tok == want_tok, f"token 取数正确（{why}）", (tok, c.amount_of(text)))
check(c.amount_of("免费领取20美元额度，月费99美元")[0] == 20.0,
      "领取语境的 20 美元胜过价格语境的 99 美元", c.amount_of("免费领取20美元额度，月费99美元"))
check(c.amount_of("需买7元套餐")[0] == 0.0, "价格语境的金额不算送的额度", c.amount_of("需买7元套餐"))
check(c.amount_of("充值送20刀")[0] == 20.0, "价格词后面跟着发放动词的照旧算", c.amount_of("充值送20刀"))

print("\n== 量值：字母紧贴的数量与「个亿」（2026-10-16 实测盲点）==")
# 真实语料里这三条都被旧边界挡掉，精选筛查时全部落进「量值不可判」：
# `注册送gpt2亿`、`再发一次dp4.1f2亿token`、`大概还有60个亿`。
for text, want_tok, why in [
        ("注册送gpt2亿", 2e8, "品牌名紧贴数量（gpt2亿）"),
        ("下课了好伤心，那就再发一次dp4.1f2亿token", 2e8, "模型名紧贴数量（f2亿）"),
        ("臭鸡蛋glm-5.3-flash，大概还有60个亿，到9点", 6e9, "口语写法「60个亿」")]:
    check(c.amount_of(text)[1] == want_tok, f"量值取数正确（{why}）", c.amount_of(text))
check(c.amount_of("注册链接 https://modeloc.com/i/EXAMPLE73B")[1] == 0.0,
      "URL 邀请码里的 73B 仍不算额度（B/M 的字母边界与 token 语境不动）")
check(c.amount_of("送个100$额度的deepseek-v4.1-flash")[0] == 100.0,
      "美元符号写在数字后面的写法（100$）也算金额",
      c.amount_of("送个100$额度的deepseek-v4.1-flash"))

print("\n== 预算按尝试计数（2026-10-15 审计：失败风暴放大请求）==")
_tmp = tempfile.mkdtemp(dir=os.path.dirname(os.path.abspath(__file__)) or ".")
_old_cache = c.CACHE_DIR
c.CACHE_DIR = _tmp
try:
    calls = []
    def _broken(url, timeout=20):
        calls.append(url)
        raise OSError("injected outage")
    rows_fail = [{"id": str(i), "source": "linuxsb_首页", "url": f"https://linux.sb/topic/{i}",
                  "title": "公益站 送额度", "created_at": "2026-10-15T09:00:00+00:00"} for i in range(1, 5)]
    n = c.annotate(rows_fail, _broken, limit=1, delay=0, timeout=1, log=lambda _m: None)
    check(n == 0 and len(set(calls)) == 1 and len(calls) == 2,
          "limit=1 时只尝试 1 行（失败重试一次），不再横扫全批", (n, len(calls), len(set(calls))))

    calls.clear()
    rows_cap = [{"id": str(i), "source": "linuxsb_首页", "url": f"https://linux.sb/topic/{i}",
                 "title": "公益站 送额度", "created_at": "2026-10-15T09:00:00+00:00"}
                for i in range(1, c.PER_SOURCE_CAP + 3)]
    c.annotate(rows_cap, _broken, limit=1000, delay=0, timeout=1, log=lambda _m: None)
    check(len(set(calls)) == c.PER_SOURCE_CAP,
          "单源上限按尝试计数（失败也占额度）", (len(set(calls)), c.PER_SOURCE_CAP))

    calls.clear()
    def _unusable(url, timeout=20):
        calls.append(url)
        return "<html>challenge page</html>" + " " * 200
    rows_ld = [{"id": str(i), "source": "linuxdo_welfare", "url": f"https://linux.do/t/topic/{i}",
                "title": "linux.do 帖", "created_at": "2026-10-15T09:00:00+00:00"} for i in range(1, 6)]
    c.annotate(rows_ld, lambda *a, **k: (_ for _ in ()).throw(OSError()), limit=0, delay=0,
               stealth_fetch=_unusable, stealth_limit=1, stealth_delay=0, log=lambda _m: None)
    check(len(calls) == 1, "隐身通道「200 但正文不可用」也占额度、也计入熔断", len(calls))

    print("\n== 正文不可用也写 offer（thin），不再每轮占预算（2026-10-16 实测）==")
    # 52 行「回复可见 / 只有一张图 / 就一句标题」的短正文，旧行为是「不写 offer」，
    # 于是每轮都被当成「没判过」重新排进预算 —— 一轮 60 条预算被它们吃掉大半。
    calls_thin = []

    def _hidden(url, timeout=20):
        calls_thin.append(url)
        return '<div class="post-readability-content">请回复后查看内容。</div>'

    rows_thin = [{"id": "9", "source": "linuxsb_首页", "url": "https://linux.sb/topic/9",
                  "title": "送个100$额度的deepseek-v4.1-flash", "created_at": "2026-10-15T09:00:00+00:00"}]
    n_thin = c.annotate(rows_thin, _hidden, limit=5, delay=0, timeout=1, log=lambda _m: None)
    check(n_thin == 0, "正文不可用不计成功（隐身通道的熔断与计数保持原样）", n_thin)
    check(rows_thin[0].get("offer", {}).get("thin") is True
          and rows_thin[0]["offer"].get("v") == c.OFFER_V
          and rows_thin[0]["offer"].get("amount") == "$100",
          "但仍写 offer（量值从标题抽：thin + $100）", rows_thin[0].get("offer"))
    calls_thin.clear()
    n_thin2 = c.annotate(rows_thin, _hidden, limit=5, delay=0, timeout=1, log=lambda _m: None)
    check(n_thin2 == 0 and not calls_thin, "下一轮不再重判、也不再重抓（TTL 到了才重判）",
          (n_thin2, calls_thin))

    print("\n== offer 过期后会重判（缓存过期不该只让缓存失效）==")
    import time as _time
    now = _time.time()
    rows_exp = [{"id": "1", "source": "linuxsb_首页", "url": "https://linux.sb/topic/900",
                 "title": "旧行", "created_at": "2026-10-15T09:00:00+00:00",
                 "offer": {"v": c.OFFER_V, "key": "old.example", "site": "old.example",
                           "amount": "", "brand": "", "at": int(now - 25 * 3600)}}]
    calls.clear()
    def _new_body(url, timeout=20):
        calls.append(url)
        return '<div class="post-readability-content">https://new.example/reg 领取测试额度三十天有效。"' + '"' * 0 + "</div>"
    n = c.annotate(rows_exp, _new_body, limit=5, delay=0, timeout=1, now=now + 1, log=lambda _m: None)
    check(n == 1 and rows_exp[0]["offer"]["key"] == "new.example",
          "过期的 offer 会被重判并换成新身份", (n, rows_exp[0]["offer"].get("key")))
    calls.clear()
    n2 = c.annotate([{"id": "2", "source": "linuxsb_首页", "url": "https://linux.sb/topic/901",
                      "title": "新行", "created_at": "2026-10-15T09:00:00+00:00",
                      "offer": {"v": c.OFFER_V, "key": "fresh.example", "site": "fresh.example",
                                "amount": "", "brand": "", "at": int(now)}}],
                    _new_body, limit=5, delay=0, timeout=1, now=now + 1, log=lambda _m: None)
    check(n2 == 0 and not calls, "没过期的 offer 不重判（否则每天白跑一遍全库）", (n2, calls))
finally:
    c.CACHE_DIR = _old_cache
    shutil.rmtree(_tmp, ignore_errors=True)

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
         "title": "t", "url": "u",
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
# 输入变了（URL 或标题被编辑过）→ 不继承旧身份，留给正文层重判
_url_changed = {"id": "1", "title": "t", "url": "u2", "source": "linuxsb_首页"}
fm.carry_forward(_prev, _url_changed)
check("offer" not in _url_changed, "carry_forward：URL 变了不继承旧 offer（换推广站/改活动）")
_title_changed = {"id": "1", "title": "t2", "url": "u", "source": "linuxsb_首页"}
fm.carry_forward(_prev, _title_changed)
check("offer" not in _title_changed, "carry_forward：标题变了不继承旧 offer")

# ---------------------------------------------------------------- 合并键：活动名优先（2026-10-24）
# 由头：zcode 1024"端内领 5 亿"的活动没有任何网页链接（8+ 条帖跨 3 个源，各算各的卡）；
# hyxapi 的站点域名是 hyxware.com，转帖标题只写 "Hyxapi公益站" —— 活动名与域名不同名。
# 规则：key = 活动名（别名命中，链接帖与标题帖同一个键）→ 站点 → None。
# 品牌与量值仍然不参与（那是两条有语料教训的明文决策，不能因为这次改动回潮）。
check(c.offer_of("muse 送 30 亿", "", ("https://muse.ai/x",))["key"] == "muse",
      "合并键：muse 的链接帖 → 活动名 muse（不再用域名当键）")
check(c.offer_of("鸡蛋！！MUSE 电报领 30 亿", "", ("https://t.me/satudigitalbot",))["key"] == "muse",
      "合并键：muse 的另一个入口（t.me 机器人）→ 同一个键 muse（此前一分为二）")
check(c.offer_of("muse AI免费领鸡蛋邀请码", "", ())["key"] == "muse",
      "合并键：无链接、标题认得出活动 → 也能并进 muse")
check(c.offer_of("HyxAPI新公益站，签到10刀以上", "", ("https://hyxware.com/auth?mode=register&aff=BBpX",))["key"] == "hyxapi",
      "合并键：hyxware.com 的链接帖 → 活动名 hyxapi")
check(c.offer_of("Hyxapi公益站", "", ())["key"] == "hyxapi",
      "合并键：只在标题里写 Hyxapi（linux.do 那条未判定帖）→ 同一个键 hyxapi")
check(c.offer_of("zcode 1024端内送5亿token额度", "", ())["key"] == "zcode",
      "合并键：端内活动无链接（zcode）→ 标题里的活动名当键")
_zk = c.offer_of("快去白嫖zcode领取5亿可用glm5.3", "", ())
check(_zk["key"] == "zcode" and "5亿" not in _zk["key"] and "glm" not in _zk["key"],
      "合并键：量值与品牌仍不参与（zcode 就是键，5亿/glm 都不是）", _zk)
check(c.offer_of("某站注册送 10 刀", "", ("https://dzhsmartai.com/",))["key"] == "dzhsmartai.com",
      "合并键：没进别名表的站，行为不变（key == 站点）")
check(c.offer_of("免费领取5亿token庆祝1024节", "", ())["key"] is None,
      "合并键：无链接且标题认不出活动 → 仍是 None（等正文判定，不瞎猜）")

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
    check(rows[0].get("offer", {}).get("key") == "muse",
          "1 号帖写入 offer key（别名活动 → 活动名）", rows[0].get("offer"))
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
