#!/usr/bin/env python3
"""
ai-welfare-daily fetcher
Scrapes multiple sites for AI 中转站福利 posts and emits a JSON lines file.
Sources:
  - linux.sb: /forum/2, /forum/8, /index.php?sort=lucky, /index.php?sort=card, /
  - baipiao.org: /bbs/api/discussions
  - nodeloc.com: /latest.json, /c/welfare/12.json
  - vibex.iflow.cn: /c/4.json (心流AI社区 补给站)
  - linux.do: /c/welfare/36 (via Scrapling StealthyFetcher, Cloudflare protected)
  - nextbuf.com: /, /page-N (NB 社区；列表只有相对时间，见 fetch_nextbuf)
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import tempfile
import urllib.error
import urllib.request

BASE_LINUXSB = "https://linux.sb"
BASE_BAIPIAO = "https://baipiao.org"
BASE_NODELOC = "https://www.nodeloc.com"
BASE_LINUXDO = "https://linux.do"
BASE_VIBEX = "https://vibex.iflow.cn"
BASE_NEXTBUF = "https://www.nextbuf.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ai-welfare-daily/1.0; +https://github.com/xiaopeng66/ai-welfare-daily)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

def _atomic_write(path: str, text: str) -> None:
    """Write through a temp file in the same directory, then os.replace().

    A run killed mid-write (machine shutdown, task kill -- these run at boot
    and logon) must never leave a truncated store or page behind. The store is
    the only copy of the merged history, so a half-written file is data loss.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# Use Clash proxy for all HTTP requests
_PROXY_HOST = os.environ.get("CLASH_PROXY_HOST", "127.0.0.1")
_PROXY_PORT = int(os.environ.get("CLASH_PROXY_PORT", "7897"))
_PROXY_URL = f"http://{_PROXY_HOST}:{_PROXY_PORT}"

# Per-run counter of failed source fetches; used to refuse writing a store that
# silently lost a whole source (e.g. CI without a working proxy).
FETCH_ERRORS = []


def _write_if_changed(path: str, text: str) -> bool:
    """Write only when the content differs. Returns True if the file changed.

    Every run rebuilds the whole store, so without this the bytes changed on
    every single run (one fresh fetched_at per row) and the scheduled job pushed
    a commit plus a Pages deploy even when no post was new.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    _atomic_write(path, text)
    return True


def fetch(url: str, timeout: int = 20) -> str:
    proxy_handler = urllib.request.ProxyHandler({"http": _PROXY_URL, "https": _PROXY_URL})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        if _PROXY_HOST not in (None, "", "127.0.0.1", "localhost"):
            raise
        # In GitHub Actions or when proxy is unavailable, retry direct
        direct_opener = urllib.request.build_opener()
        req2 = urllib.request.Request(url, headers=HEADERS)
        with direct_opener.open(req2, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")


def extract_published_time(html: str) -> str | None:
    """Real publish time from a topic page's meta tags (pure, testable).

    Either attribute order is legal HTML; requiring property-then-content made
    the extraction silently return None on a reversed tag, and such a topic can
    never earn a verified publish time, so its detail page is re-fetched on
    every single run.
    """
    match = re.search(
        r"<meta[^>]+property=[\"']article:published_time[\"'][^>]+content=[\"']([^\"']+)[\"']"
        r"|<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']article:published_time[\"']",
        html,
    )
    if not match:
        return None
    published = match.group(1) or match.group(2)
    try:
        return datetime.fromisoformat(published).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def fetch_topic_published_time(topic_id: str) -> str | None:
    """Fetch a topic page and extract the true published time from meta tags."""
    try:
        return extract_published_time(fetch(f"{BASE_LINUXSB}/topic/{topic_id}", timeout=15))
    except Exception:
        return None


# source 字段前缀 → 抓取报错信息里的源标识，用来判断「这一轮到底看没看到这个源」。
_HOST_MARKERS = {
    "linuxsb": "linux.sb",
    "baipiao": "baipiao",
    "nodeloc": "nodeloc",
    "linuxdo": "linux.do",
    "vibex": "vibex",
    "nextbuf": "nextbuf",
}


def host_of(source: str) -> str | None:
    """把一行的 source 归到源（linuxsb_福利放送 → linuxsb）。"""
    for prefix in _HOST_MARKERS:
        if str(source).startswith(prefix):
            return prefix
    return None


def unhealthy_hosts(fetched: list) -> set:
    """这一轮没看成的源：一个 topic 都没解析出来，或报了抓取错。

    这些源名下的行绝不因为「已不在列表里」被删 —— 那时缺席只说明我们没看到，
    不说明帖子没了。粒度刻意取粗（某源有一个板块失败就算整源不健康）：
    删错不可恢复，少删一次只是多留一轮。
    """
    seen = {host_of(t.get("source", "")) for t in fetched} - {None}
    bad = {p for p in _HOST_MARKERS if p not in seen}
    for err in FETCH_ERRORS:
        for prefix, marker in _HOST_MARKERS.items():
            if marker in err:
                bad.add(prefix)
    return bad


def probe_deleted(url: str, timeout: int = 8) -> bool:
    """True 仅当该 URL 回 404/410 —— 一个明确的「源站已删除」。

    其它一切结果（200、WAF 的 403、5xx、超时、DNS 失败）都返回 False。这个不对称
    是刻意的：删错不可恢复（行一离开 store，就再没有哪一轮会去重抓它），
    而多留一轮毫无代价。
    """
    if not url:
        return False
    try:
        fetch(url, timeout=timeout)
        return False
    except urllib.error.HTTPError as e:
        return e.code in (404, 410)
    except Exception:
        return False


CATEGORY_KEYWORDS = {
    "中转站": ["中转站"],
    # 每个分类都要求一个「这个分类特有的标的词」，绝不接受通用动作词。
    # 反例（都在实测里出现过）：`额度` 靠裸 `送` 给 18 条不含「额度/刀」的帖打上标签；
    # `兑换码` 靠裸 `code` 命中 `zcode` / `codex` —— zcode 的臭鸡蛋、claude code 公益站
    # 全被打成「兑换码」。通用词（送/免费/抽）只能出现在闸门里，不能出现在分类里。
    # 「免费使用」故意不放这里：它同时是 免费放粮 的词，同一标题必然双标。
    # 公益站 有「公益」这个强特征词，不需要靠它。
    "公益站": ["公益站", "公益", "零门槛"],
    "鸡蛋": ["鸡蛋"],
    "兑换码": ["兑换码", "邀请码", "注册码", "cdk"],  # score_topic lowers ASCII first (CDK also counts)
    "额度": ["额度", "刀", "美刀", "余额"],
    "体验金": ["体验金", "赠金", "credit",
               # 2026-10-05：`7日体验卡`、`24H 体验卡再来一批`、`Claude Pro 一周体验券 × 3`
               # —— 体验卡的写法和体验金同义，原先只认「体验金」三字，这类帖拿不到标签。
               "体验卡", "体验券"],
    "抽奖": ["抽奖", "盲盒", "中奖", "欧皇"],
    # 无标签帖曾积到 64 条（占 24%），全是「白嫖/羊毛/免费/放粮/号池」这类直接白拿的帖，
    # 一个既有分类都套不上。补这一类后吸收 29 条、剩 20 条（7.5%）。
    # 2026-10-06 移除裸号池：linux.sb/25119、/24798 明示全部官方原价。
    # 免费号池仍由 福利/免费/蹬 等独立信号打标，不为清零无标签而错标。
    "免费放粮": ["白嫖", "羊毛", "薅", "放粮", "放糧", "不花钱", "白送", "白给",
                 "免费", "免费用", "免费使用", "领取", "领",
                 # 2026-10-05：`token滞销了 发给饼友们蹬吧`（滞销=库存送人，源站黑话）、
                 # `可蹬gpt-6.1-sol`/`开蹬`/`速蹬`/`猛蹬`/`免费瞪`（蹬、瞪同音变体，
                 # 在这类站的语境里 = 免费用额度）。实测语料里 12 条含「蹬」的过闸帖
                 # 全部是白拿帖，0 错标；漏掉的是 3 条 0 标签帖（分类筛选筛不出它们）。
                 "滞销", "蹬", "瞪"],

    # 用户要求新增（2026-10-04）：专门收集低价/优惠的中转渠道。
    # linux.sb/25107 等同标题报价：低至 ¥0.0211/刀起，旧表只有额度标签。
    "优惠渠道": ["折扣", "折", "特价", "低价", "优惠", "首充", "起充", "返利", "倍率",
                  # 闸门 OFFER 里有「返现/充值」，分类里漏了对应的优惠写法，于是这些帖
                  # 能过闸却拿不到标签：`充值双倍`、`最高返50%`、`充值最高享20%返赠`。
                  "返现", "返赠", "双倍", "翻倍", "加赠", "低至"],
}

# 分类的**形状**补充：有些信号是写法而不是词。中转站报价的主流写法是倍率
# （`Claude 0.09x起`、`国模 0.25x`），词表抓不住 —— 往词表里加 `0.09x` 没用，
# 加裸 `x` 会把什么都打上。约定与 `_bounded` 一致：x 后面不许跟字母。
# 不能用 `\b`：`0.09x起` 里 x 后面是汉字，Python 认为两者之间没有边界。
CATEGORY_PATTERNS = {
    # 必须带小数点：`3x-ui`（面板）和 `claude 20x`（订阅档位）都会误命中裸 `3x` / `20x`，
    # 而真实报价一律写成 `0.09x` / `0.25x`。
    "优惠渠道": (re.compile(r"\d+\.\d+x(?![a-z])", re.I),
                 # 价格宣告的第四种形状（2026-10-05）：`价格不到1分钱`。
                 # 语料里 1 条命中，正是靠这条才拿到「优惠渠道」（原先 0 标签）。
                 re.compile(r"不到\s*[¥$]?\d")),
    # 无标签帖 18/265（6.8%）逐条看下来，剩下的都是**词表里一个词都不含**的写法，
    # 加词无从加起，只能按形状认。三类：
    #   ① 金额/额度形状：`注册送70`、`GLM-5.3 可获得 2000万Tokens`、`1000$ 130积分兑换`
    #   ② 纯「福利」+站名：`Fox AI国庆福利来了`、`智链AI-双节福利`
    # 金额那一类救 7 条，泛福利那一类救 7 条，合起来把无标签压到 4 条（1.5%）。
    # 都用 (?<![a-z]) / (?![a-z]) 与 _bounded 保持一致的边界约定。
    "额度": (
        # 只认「明确的发放/获得」形状，不认裸金额 —— 裸 `N元` 分不清额度与标价，
        # 实测 `月费9.9元套餐`、`年费199元`、`1元试用`、`会员价199元` 全中，
        # 想靠前后文排除会越补越复杂（费写在数字前的、写在后的、售价/价格/月付…）。
        # 三条都要求「送/得/给/领/赠 + 数字」或币种符号或万级 token。
        # linux.sb/19953 的 20% 曾回溯匹配成 2，须阻止截断数字/小数并挡全角％。
        re.compile(r"(?:送|得|给|领|赠)\s*[¥$]?\s*\d+(?:\.\d+)?(?![\d.]|\s*[%％])"),
        re.compile(r"[¥$]\s*\d+(?![a-z])|\d+\s*[¥$](?![a-z])"),
        re.compile(r"\d+\s*万\s*[Tt]okens(?![a-z])"),
        # 亿级（2026-10-05）：`发20亿DeepSeek对冲一下`、`200亿 deepseek-v4-flash 精品鸡蛋`。
        # 刻意不带 (?![a-z])：亿 是汉字，后面直接粘模型名是常态（`20亿DeepSeek`），
        # 加了前瞻反而全漏。整语料命中 6 条，逐条核对全是真发放帖。
        re.compile(r"\d+(?:\.\d+)?\s*亿"),
        # `300W` = 300 万 tokens 的写法（`七牛云新户可用300W deepseek-v4-flash`）。
        # 只有 1 条帖命中，且这类写法不用 W 就没法写 —— 词表无从下手。
        re.compile(r"\d+\s*[Ww](?![a-z])"),
    ),
    "免费放粮": (
        # 「福利」是只在免费放粮里出现的泛词。注意这条**本身不校验 AI 相关性** ——
        # 「双十一福利」单看也会中。它之所以安全，是因为 score_topic 只对**过了闸门**的帖跑，
        # 而闸门已要求 AI 领域词 ∧ 发放信号；「XX论坛福利大放送」根本到不了这里。
        # 别把这个前提改掉（test_parsers 有断言盯着这层耦合）。
        # (?!站) 是为了不抢 公益站 的活（`公益福利站` 是公益站）。
        re.compile(r"福利(?!站)", re.I),
        # 「送/赠 + …+ api/key/token/额度」的形状（2026-10-05）：`送一个阶跃星程的api`
        # 一个分类词都不含（没有金额、没有「免费/送」这类分类词里的写法），是 6 条
        # 0 标签帖之一。放宽到「送 一个/个/点/些 + 8 字内 + 标的物」，实测命中 14 条
        # 过闸帖，全部是真白拿帖（`送300刀cc额度`、`【送额度】kiro企业版`…），0 错标。
        re.compile(r"(?:送|赠|白给|白送)\s*(?:一个|个|点|些)?[^，。！]{0,8}?(?:api|key|token|额度)", re.I),
    ),
}

# ---------------------------------------------------------------------------
# 相关性闸门：两级规则
#
# 为什么不是一张词表：原来的单层闸门（8 个词，命中即收）有两个反向缺陷，
# 在 2026-10-04 的全量语料（607 抓取 / 509 唯一标题）上量过：
#
#   ① 漏真货 194 条。闸门只认「中转站/公益/鸡蛋/兑换码/额度/体验金/抽奖」，
#      而这类站的发帖黑话是「注册送 1 刀」「0.0001 倍率」「爽蹬 $1000 刀」
#      「放粮」「号池」「token 滞销」——一个闸门词都不含，整条被丢。这些不是
#      边缘情况，是主力内容：榜单上最热的免费额度帖恰好都不含那 8 个词。
#   ② 收噪音 24 条，全部来自「抽奖」。VPS/小鸡/esim/TG号/域名/会员/代理IP 的
#      抽奖帖也含「抽奖」，精度只有 38%（39 条里 15 条与 AI 有关）。
#
# 所以拆成两个必居其一的入口：
#   A. 自足词 SELF_SUFFICIENT —— 词本身即「AI 福利站 / 放额度行为」，单独成立。
#   B. AI 领域词 AND 福利行为信号 —— 「这条帖在讲 AI 额度」且「它在送/放开」。
# 单说领域（"Claude 注册防封经验"）不算福利；单说送（"抽 3 台香港小鸡"）不算 AI。
#
# 验收（同一份语料）：保留 119 → 183，剔除的 34 条经逐条人工核对全部与 AI API
# 无关（VPS/esim/TG号/域名/会员/称号/挂机宝），且残留的 4 条是「额度给的太少了」
# 这类抱怨帖，不是福利投放 —— 剔除正确。
# ---------------------------------------------------------------------------

# A. 自足词：命中即收，无需再看别的。
#    注意「额度」故意不在这里：它只说明这条帖在讲额度，不说明在送额度 ——
#    「额度给的太少了」「go的额度也下降了」是抱怨帖，必须走 B 的 AND 判定。
SELF_SUFFICIENT_KEYWORDS = [
    "中转站", "公益", "鸡蛋", "体验金", "号池", "放粮", "放糧", "兑换码",
]

# B-1. AI / 额度领域词。用 ASCII 前后瞻而不是 \b：Python 里中文也算 \w，
# 所以 r"\bgpt\b" 匹配不到「月gpt」（"月" 与 "g" 之间没有边界）——
# 实测这会漏掉 "【抽奖】吐血福利免费送4个月gpt plus会员" 这类标题。
_ASCII = r"[a-z0-9]"


def _bounded(token: str) -> str:
    """ASCII 边界版的关键词：左不许字母数字、右不许字母。

    右侧刻意只挡字母（不挡数字），这样 "GPT6" / "ds4.1" 这类型号名仍然命中。
    """
    return rf"(?<!{_ASCII}){token}(?![a-z])"


def _prefix_bounded(token: str) -> str:
    """只卡左边界的长名字：右侧允许直接粘版本号/后缀。

    `deepseekv4flash` / `DeepSeekharness` 这类写法里模型名后面紧跟字母，
    `_bounded` 的 `(?![a-z])` 会把它们整条挡掉（实测漏 3 条真货）。只对足够长、
    不可能是别的英文单词片段的名字放开；短词（ai/ds/glm/gpt…）保持严格边界。
    """
    return rf"(?<!{_ASCII}){token}"


DOMAIN_KEYWORDS = [
    _bounded("api"), _bounded("key"), _bounded("cdk"), _bounded("ai"),
    _bounded("llm"), _bounded("ds"),
    "中转", "额度", "token", "模型", "倍率", "分组", "邀请码",
    # 「积分」刻意不在这里：它在中文论坛绝大多数指论坛自己的积分体系
    # （`发点积分` / `囤积分` / `积分抽奖中奖概率降低了？`），当成 AI 领域词会让
    # 整类闲聊通过 AND 判定。实测移出后 store 掉 7 条，其中 6 条正是这类闲聊，
    # 唯一代价是 `【RelayFor】突发积分`（纯站名帖，属于站名白名单该管的范围）。
    # 「充值」不在领域词里 —— 它是**行为信号**（已移入 OFFER），不是 AI 指标：
    # 放在这里会让「话费充值9折优惠」「X会员低价充值 3个月20」整类非 AI 充值帖过关
    # （实测：移出后语料只掉 1 条，而那条正是 X会员低价充值）。
    "余额", "赠送", "赠金", "签到",
    _bounded("gpt"), _bounded("glm"), _bounded("grok"), _bounded("qwen"),
    _bounded("kimi"), _bounded("codex"), _bounded("cursor"), _bounded("astra"),
    _bounded("sonnet"), _bounded("opus"), _bounded("kiro"), _bounded("nvidia"),
    _prefix_bounded("claude"), _prefix_bounded("deepseek"), _prefix_bounded("gemini"),
    _prefix_bounded("openai"), _prefix_bounded("nemotron"), _prefix_bounded("longcat"),
    _prefix_bounded("antigravity"),
    # `grokheavy`（无空格写法）：`快过期的 grokheavy 大家敞开来用` 是语料里唯一一条
    # 因为词边界被整条丢掉的帖 —— `grok` 用 _bounded，右边界不许跟字母，「heavy」把它挡了。
    # 用 _prefix_bounded 而不是把 grok 整体放宽（放宽后语料上 0 收益，只会扩大误命中面）。
    _prefix_bounded("grokheavy"),
    "刀", "蹬", "美刀", "美元",
    # vibex/6740: require a 万/亿 quantity for the source spelling tokey.
    r"\d+\s*(?:亿|万)\s*tokey(?![a-z])",
    "国模",  # 「新站开业，国模免费用」——国产模型的黑话，不含「模型」二字
]

# B-2. 福利行为信号：这条帖在「送 / 放开 / 打折」某个东西。
#    「注册」留在这里是必要的（去掉会丢 5 条真货：注册送10刀 / github注册15刀…），
#    它的教程噪音由下面的 HOWTO 模式挡掉。
#
#    「抽」「蹬」是实测补进来的：只用「抽奖」做子串会漏掉「先抽个奖叭」「抽50个10¥余额」
#    （中间隔了字），放开成单字「抽」后净增 3 条、0 噪音；「蹬」在黑话里=免费用额度，
#    补进来净增 8 条真货（「爽蹬$1000刀」「【猛蹬】claude顶级模型不花钱」），0 噪音。
OFFER_KEYWORDS = [
    "免费", "送", "赠", "白嫖", "抽奖", "抽", "兑换", "邀请", "注册", "领取", "领",
    "福利", "试用", "优惠", "折", "限时", "羊毛", "红包", "纳新", "撸", "抢",
    # 「发」要加否定前瞻：裸「发」会命中 开发 / 发现 / 发布（「发现一免费 DeepSeek」
    # 「报! 发现1不错GPT,Grok中转」）。但不能直接删 —— 删掉「又发1亿token」「发鸡蛋啦」
    # 「发点999刀CDK」全丢（实测 8 条真货）。所以只挡住明确的非发放搭配。
    # 「分」用**后视**而不是前瞻：`木马分发器` 里是「分」在「发」之前，前瞻看的是「发」后面
    # 那个字（器），根本挡不住 —— 2026-10-05 实测发现写反了方向。
    # 分发/分开/分析都是非发放搭配，而真实的「发给饼友们蹬吧」「再发点999刀CDK」不受影响。
    "新用户", "获得", r"(?<!分)发(?!现|布|表|出|明|射|展)", "蹬", "薅", "邀请", "返现", "低价", "特价", "首充", "不花钱",
    # 价格/充值类信号：用户 2026-10-04 要的「优惠渠道」分类靠它落地。没有它，
    # 纯价格表帖（`[NachoNekoAPI] 0.01x DeepSeek 不降智不掺水 1:1充值`、
    # `【烧饼换用量】Zynk API (Paid) | 国模 0.25x… 一元起充`）一个 offer 词都不含，
    # 整类漏掉。实测净增 2 条、0 噪音（`积分` 试过同位置：净增 0，故不加）。
    "充值", "起充", "折扣", "打折",
    # 下面两个来自 linux.do 的黑话（`额度快刷新了, GLM5.3搞起来`、`享用￥1000api额度`）：
    # linux.do 在本机取不到（要家宽），所以只能在四源语料上验噪音 —— 实测净增 0 条噪音。
    # 置信度低于上面那批，改闸门时优先怀疑这两个。
    "刷新", "享受",

    # 2026-10-05 语料复审（574 条唯一标题，逐条人工核对）补的放额度说法。
    # 这五个词原先一个都不在表里，整类帖子被静默丢掉 —— 实测净收 4 条真货、0 条噪音：
    #   限免   `【RelayFor】GPT-6-Luna限免5亿token~`
    #   新户   `七牛云新户可用300W deepseek-v4-flash-20260731`（表里有「新用户」，没有「新户」）
    #   低至/最低  `🔥 热门 AI 模型都来了，低至 ¥0.0211/刀起！`、`标准计费无倍率，最低0.06/MTk`
    #   体验   `GLM 5.3 flash … 7日体验卡`、`源头中转 欢迎对接使用有余额体验`
    #          （「体验金」是分类词，闸门侧原先漏了这一半说法）
    #   不到   `gpt的1k生图价格不到1分钱`（价格宣告的第三种写法）
    "限免", "新户", "低至", "最低", "体验", "不到",
]

# 闸门也有「形状」规则，和分类的 CATEGORY_PATTERNS 同理：报价/发放的主流写法是
# 形状而非词，词表加不进去（加 `0.09x` 没用，加裸 `x` 什么都中）。
# 2026-10-05 在 574 条唯一标题上逐条验证：每条形状新收的都是真货，且形状只替代
# OFFER 一侧 —— 领域词仍必须命中，所以「裸金额 + 无 AI 词」的帖子进不来。
OFFER_PATTERNS = (
    # 倍率报价：`掺水司马API GPT直KEY现在0.35x`。分类里已有同名形状（管打标），
    # 闸门原先没有 → 这类帖连门都进不了。
    r"\d+\.\d+x(?![a-z])",
    # linux.sb/25158: numeric 倍率 is a price offer without a giveaway verb.
    r"(?:\d+(?:\.\d+)?\s*倍率|倍率\s*\d+(?:\.\d+)?)",
    # 万/亿级 token 发放：`阶跃星辰stepfun 4亿token`、`限免5亿token`
    r"(?:\d+\s*亿|\d+\s*万)\s*[Tt]okens?",
    # 金额：`gpt系列500刀`、`随时跑路站50$ key*3`、`低至 ¥0.0211/刀起`
    r"[¥$]\s*\d+(?![a-z])",
    r"\d+\s*(?:刀|美刀|美元|¥|\$|块)(?![a-z])",
    # 「白给」的动作形状：`大家敞开来用` —— 词表收不下「随便/敞开 + 来 + 用」这类
    # 动词+方向补语（实测 `敞开` 单独入表匹配不到，因为它后面跟的是「来」）。
    r"(?:随便|敞开|尽管|随意)\S{0,3}(?:用|蹬|拿|撸)",
)

# B-3. 教程 / 抱怨意图：出现即否决。
#    这些帖同时含领域词和福利词（"注册防封经验"、"额度也下降了"），但它们是
#    在教怎么用 / 在抱怨，不是在放额度。实测这 4 条正是仅靠 AND 判定无法区分
#    的那批，逐条核对后确认都不该上站。
_NOT_FREEBIE_KEYWORDS = [
    "经验", "教程", "方法", "攻略", "指南", "怎么", "如何", "防封",
    "太少了", "用不起", "下降了", "涨价",
    # 「测评」= 成本分析帖（`【阳仔测评】…token成本和落地成本`），靠裸 `发` +
    # `deepseek` 混过了 AND 判定，实际一个额度都没发。
    # 「求教」= 提问帖（`求教SenseNova免费deepseek的429规则是什么`），靠 `免费` +
    # `deepseek` 过关，但它在问规则，不是放福利。
    # 这两个都排在自足词之后，所以自带 中转站/公益/鸡蛋 等强特征的真货不受影响
    # （实测：加词后只有这 2 条被剔，另外 4 条含「方法/教程」的真货照常保留）。
    "测评", "求教",
    # 2026-10-05 复审补的第三批意图否决，每条都在语料里点到 1 条真噪音、0 误伤
    # （「没有」类的索要帖：自足词在它之前判定，含 中转站/公益/鸡蛋 的真货不受影响）：
    #   问句结尾   `codex订阅100$还是200$还是500$？` —— 金额形状放开后，问价帖会靠
    #              「100$」混进来；问号收尾 + 领域词是「在提问」的可靠形状
    #   （假的）   `可用claude-fable-5-1 还有3000刀（假的）` 是打假帖，不是福利投放
    #   ^求        `求一个 BoxyBSD 邀请码`。用行首锚点而不是裸「求」：裸词会误伤
    #              需求/要求/诉求
    #   有没有     `有没有大佬给一个linux do的邀请码，我想注册几个公益站`
    #   技巧       `新Muse邀请码，可参考的注册技巧和注意事项`，与 教程/攻略 同类
    #   用不了     `这个站的余额回收调整，免费的模型经常用不了`，与「用不起」同类
    #   账单/花费/花了/扣款/扣费 —— 配合新增的金额形状做防御（本轮语料 0 效果；
    #              记在这里是因为它们正是「N刀」类标题里最可能混进来的花费陈述）
    "用不了", "账单", "花费", "花了", "扣款", "扣费", "技巧", "有没有",
    r"^求", r"[（(]\s*假的?\s*[)）]",
]

# 问句收尾的单独处理（不进上面那张表，因为它带条件）。
# 标题用问号是中文论坛的修辞习惯：`【RelayFor】再发点999刀CDK？`、`免费送gpt额度没人要吗？`
# 都是真发放帖，一律否决会漏抓（2026-10-05 实测 2 条）。所以只在标题不含**发放动词**时
# 才判为「在提问」——保护集只收动作词，刻意不收「免费」：`claude-sonnet-5-5-high 免费？？`
# `网页版？？` 是纯提问（没有一个动作词），仍要否决。
_QUESTION_TAIL_RE = re.compile(r"[?？]\s*$")
_GIVEAWAY_VERB_RE = re.compile(r"送|赠|发|领|抽|兑换|cdk|白嫖|放粮|放糧|蹬|瞪|号池", re.I)

# B-4. 标的物维度：出现这些词说明「发出去的东西不是 AI 用量」。
#
#     这是本轮补上的第三个维度。此前判定只问「出现了什么词」，从不过问送的是什么，
#     于是「送点积分」「抽个 TG 号」「白嫖一台香港小鸡」与「送 10 刀额度」在结构上
#     完全等价，全部照收。词表取自 nodeloc / NodeSeek 的服务器黑话（小鸡=vps、
#     杜甫/毒妇=独服、玉米=域名）与实测混进来的噪音族。
#
#     刻意不收录的两个词：
#       「账号」—— 太宽，会误杀「进群找管理员把账号发到群里改倍率」这类真福利；
#       「富可敌国」—— linux.do 的用户等级徽章，几乎每条推广帖都带，
#                      实测会误杀「【富可敌国】…尔信中转站codex-0.12x｜抽奖+充值返赠」。
_NONAI_TARGET_KEYWORDS = [
    # 服务器/主机
    "小鸡", "母鸡", "杜甫", "毒妇", "独服", "服务器", "云服务器", "挂机宝", "家宽",
    "探针", "大盘鸡", "节点", "机场", "宽带", _bounded("vps"), _bounded("nat"),
    # 域名 / 存储
    "域名", "玉米", "备案", "虚拟主机", "图床", "网盘", "云盘",
    # 影视站：`基于MoonTVPlus的影视公益站` 靠自足词「公益」免检进来（自足词命中即收，
    # 不看标的物）。自足词越强，越需要这一层替它把关 —— 「影视」不在 AI 用量范畴。
    # 不误伤：语料里 AI 漫剧类（`AI漫剧平台…发放额度`）写的是「漫剧」，不是「影视」。
    "影视",
    # nodeloc/112419 provides proxy addresses, not model inference.
    _bounded("socks5"),
    # 非 AI 的数字商品 / 账号 / 卡
    "tg号", "电报号", "美区号", "苹果id", "抢苹果", _bounded("steam"),
    "礼品卡", "充值卡", "代金券", "虚拟卡", "信用卡", "流量卡", "电话卡",
    _bounded("stripe"), _bounded("esim"),
    # 消费电子 / 论坛内部头衔体系
    "指纹浏览器", "称号", "等级", "元老", "鸡腿", "活跃度",
]

# B-5. 已结束的帖子：福利已开奖/领完/失效，留着就是死信息。
#     实测库里积了 24 条（`[已开奖]…`、`爽蹬$1000刀(已完)`、`…（已无）`）。
#     判定是纯函数，所以闸门和「下一轮删除」用的是同一张表。
_STALE_KEYWORDS = [
    "已开奖", "已流抽", "已结束", "已赠送", "已领完", "已失效", "已完", "已无",
    # 「无了」是源站自己标的「已发完」——《【无了】国庆节快乐 GPT6系列 速蹬》明显不可领。
    "无了",
    # 「已送出」：`送个L站青春版邀请码（已送出）`、`【阶跃星辰】送点阶跃星辰token（已送出）`
    # —— 和「已赠送」同义，原先只收了后者，这两条一直挂在榜上（2026-10-05 实测 2 条）。
    "已送出",
    # linux.sb/25116; historical linux.do/2979892 and /2981251.
    "已耗尽", "刷完啦", "已暂时无",
]

# 会员类：ChatGPT Plus / Gemini 会员是账号商品，不是中转额度 → 默认否决。
# 但「首充 $66 拿 Claude…3.3 折，会员返利还叠加」是站点的充值优惠，属于目标内容，
# 所以只有同时出现价格词和 AI 词才放行（用户 2026-10-04 决定：会员类放行）。
_MEMBERSHIP_RE = re.compile(r"会员|svip|年费|月费", re.I)
_PRICE_RE = re.compile(r"折|首充|充值|返利|优惠|特价|低价|倍率|羊毛", re.I)

_DOMAIN_RE = re.compile("|".join(DOMAIN_KEYWORDS), re.I)
# 词 + 形状：形状只补 OFFER 一侧（领域的「形状」会误伤太多，没有需要）。
_OFFER_RE = re.compile("|".join(list(OFFER_KEYWORDS) + list(OFFER_PATTERNS)), re.I)
_NOT_FREEBIE_RE = re.compile("|".join(_NOT_FREEBIE_KEYWORDS), re.I)
_NONAI_TARGET_RE = re.compile("|".join(_NONAI_TARGET_KEYWORDS), re.I)
_STALE_RE = re.compile("|".join(_STALE_KEYWORDS), re.I)


# High-confidence intent vetoes must precede self-sufficient words.
# Evidence: baipiao/641, linux.sb/25147, /25155, /25159, /21529, /24914;
# vibex/6690. Keep generic 方法/教程 handling below the strong-offer shortcut.
_REQUEST_RE = re.compile(r"^(?:求|收(?:一个|个)|帮忙推荐|有佬有)|有没有|问个问题|我能不能", re.I)
# A title can start with a rhetorical “有好用的…么” and then explicitly
# announce its own claimable grant. Do not use generic 免费/领 here: those
# words also occur in requests for a recommendation.
_RECOMMENDATION_REQUEST_RE = re.compile(r"有好用的.+[么吗嘛]", re.I)
_RHETORICAL_OFFER_RE = re.compile(
    r"有好用的[^，,。！？!?；;\n]+[么吗嘛][，,。！!；;？?\s]*"
    r"(?:我(?:来|给大家)?|本站)?(?:免费)?(?:送|赠)\s*\d+(?:\.\d+)?\s*"
    r"(?:刀|元|额度|tokens?|credits?)(?:[！!。.]|$)", re.I)
_NON_OFFER_RE = re.compile(r"自动签到面板|抽奖功能测试|对免费用户下手|免费模型缩水", re.I)


def is_relevant_title(title: str) -> bool:
    """True if a title belongs on the site: an AI-API freebie / 中转站 welfare post.

    判定顺序（先否决，再收）：

      0. 已结束（[已开奖]/已完/已无…）—— 死信息，不区分内容一律丢。
      1. 非 AI 标的（服务器/域名/卡/论坛头衔）—— 收进来的东西不是 AI 用量，
         与中转无关。这一层此前完全缺失，是「送点积分」「抽个 TG 号」
         「白嫖一台香港小鸡」能混进来的根因。
      2. 明确索要、自动签到工具、功能测试、负面消息、会员及问句检查。
      3. 自足词（中转站/公益/鸡蛋/号池/放粮/兑换码）—— 前置否决通过后收。
         它排在语境否决之前：否则「10亿token鸡蛋块领，Muse轻松注册另一种方法」
         这种带「方法」二字的真货会被误杀。
         会员默认否决，价格词和 AI 词同时存在时继续判定。
      4. 教程/抱怨意图（经验/教程/太少了…）—— 在教怎么用或在抱怨，不是在放额度。
      5. 否则要求 AI 领域词 AND 发放信号同时命中。

    Pure function: no network, no globals mutated — so the gate stays testable offline.
    """
    if not title:
        return False
    low = title.lower()
    if _STALE_RE.search(low):
        return False
    if _NONAI_TARGET_RE.search(low):
        return False
    if _REQUEST_RE.search(low):
        return False
    if _RECOMMENDATION_REQUEST_RE.search(low) and not _RHETORICAL_OFFER_RE.search(low):
        return False
    if _NON_OFFER_RE.search(low):
        return False
    # Do not let 免费/白嫖 be their own evidence of an AI domain or a positive
    # giveaway when preceded by negation (linux.sb/21525: 不白嫖).
    if "不白嫖" in low and not (_DOMAIN_RE.search(low) and re.search(r"送|领|抽", low)):
        return False
    if _MEMBERSHIP_RE.search(low) and not (_PRICE_RE.search(low) and _DOMAIN_RE.search(low)):
        return False
    if _QUESTION_TAIL_RE.search(low) and not _GIVEAWAY_VERB_RE.search(low):
        return False
    if any(kw in low for kw in SELF_SUFFICIENT_KEYWORDS):
        return True
    if _NOT_FREEBIE_RE.search(low):
        return False
    return bool(_DOMAIN_RE.search(low)) and bool(_OFFER_RE.search(low))


# 旧名字保留做兼容（含测试引用）。它现在只是词表并集，不再是判定依据 ——
# 判定请用 is_relevant_title()。
RELEVANCE_KEYWORDS = SELF_SUFFICIENT_KEYWORDS + [
    "额度", "邀请码", "抽奖", "token", "倍率", "积分", "注册", "免费",
]


class BaipiaoHTMLParser(HTMLParser):
    """Fallback parser for baipiao.org HTML listings."""

    def __init__(self):
        super().__init__()
        self.topics = []
        self._current_href = None
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href") or ""
        m = re.search(r'/bbs/d/(\d+)', href)
        if m:
            self._current_href = m.group(1)
            self._in_link = True

    def handle_data(self, data):
        if self._in_link and self._current_href:
            title = data.strip()
            if title and len(title) > 2:
                self.topics.append({
                    "id": self._current_href,
                    "title": title,
                    "url": f"{BASE_BAIPIAO}/bbs/d/{self._current_href}",
                    "created_at": None,
                })
            self._in_link = False
            self._current_href = None


class NodeLocHTMLParser(HTMLParser):
    """Extract /t/topic/N links from nodeloc.com listing pages."""

    def __init__(self):
        super().__init__()
        self.topics = []
        self._current_href = None
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href") or ""
        m = re.search(r"/t/topic/(\d+)", href)
        if m:
            self._current_href = m.group(1)
            self._in_link = True

    def handle_data(self, data):
        if self._in_link and self._current_href:
            title = data.strip()
            if title and len(title) > 2:
                self.topics.append({
                    "id": self._current_href,
                    "title": title,
                    "url": f"{BASE_NODELOC}/t/topic/{self._current_href}",
                    "created_at": None,
                })
            self._in_link = False
            self._current_href = None


def parse_topics(html: str, parser_class) -> list:
    parser = parser_class()
    parser.feed(html)
    return parser.topics


def _lsb_title(anchor_inner: str) -> str:
    """Title text of a linux.sb listing anchor.

    The daily-hot-topics block nests the title and a reply count inside one
    anchor ("免费订阅" + "近 24 小时 29 回复"), so the anchor's whole text is not
    a title. Prefer a nested element whose class mentions "title"; ordinary
    `.post-title` rows have no such child and fall through unchanged.
    """
    m = re.search(r'class="[^"]*title[^"]*"[^>]*>(.*?)<', anchor_inner, re.S)
    if m:
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if len(text) > 2:
            return text
    return re.sub(r"<[^>]+>", "", anchor_inner).strip()


def _site_of(url: str | None) -> str:
    """Registrable host of a topic URL, used to namespace topic ids.

    Every site numbers its own posts from 1, so a bare numeric id is NOT unique
    across sources: linux.sb is at ~24k while nodeloc is already past 100k, and
    the two ranges are on a collision course. A cross-site clash used to drop a
    row silently in deduplicate() or overwrite one site's row with the other's.
    """
    m = re.match(r"https?://([^/]+)", url or "")
    host = (m.group(1) if m else "").lower()
    return host[4:] if host.startswith("www.") else host


def topic_key(topic: dict) -> str:
    """Merge/dedupe key: host-qualified, and recomputable from a stored row.

    It must stay a pure function of fields the store persists (url, source, id),
    because the merge derives it again on every load. `source` is only a last
    resort for a row whose url is missing or malformed - with an empty host, two
    different sites' rows would share the key "#123" and dedupe would drop one
    without a word."""
    site = _site_of(topic.get("url")) or str(topic.get("source") or "").strip()
    return f"{site or 'unknown'}#{topic.get('id')}"


def deduplicate(topics: list) -> list:
    seen = set()
    out = []
    for t in topics:
        key = topic_key(t)
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def note_empty_source(name: str, count: int) -> None:
    """Flag a source that fetched fine but parsed to nothing.

    That is markup/endpoint drift, not an empty site: without this the source
    goes dark with the build staying green, and because the incremental merge
    keeps cached rows, nothing else in the pipeline notices either. Only page 1
    of a paginated listing is required to have items - a later page can simply
    be past the end of the board.
    """
    if count:
        return
    print(f"[warn] {name}: 0 topics parsed - markup or endpoint changed?", file=sys.stderr)
    FETCH_ERRORS.append(f"{name}: 0 topics parsed (markup/endpoint changed?)")


def score_topic(topic: dict) -> dict:
    title = topic["title"].lower()
    tags = []
    for tag, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in title:
                tags.append(tag)
                break
        else:
            if any(p.search(title) for p in CATEGORY_PATTERNS.get(tag, ())):
                tags.append(tag)
    tags = list(dict.fromkeys(tags))
    topic["tags"] = tags
    topic["score"] = len(tags)
    return topic


def parse_linuxsb_listing(html: str) -> list:
    """Rows of one linux.sb listing page. Pure: no network, so it is testable.

    One regex per row instead of one regex for the whole page: with `.*?` and
    re.S, a row that lost its <span data-performance-time> inherited the NEXT
    row's timestamp and consumed that row's link, so a single markup anomaly
    silently shifted times and dropped topics (measured: 8 rows per page).
    Here a row's timestamp is only searched for up to the next row's link, so an
    anomaly degrades to "no timestamp" instead of corrupting its neighbour.
    """
    anchor_re = re.compile(r'href=["\']/topic/(\d+)["\'][^>]*>(.*?)</a>', re.S)
    time_re = re.compile(r'data-performance-time="(\d+)"')
    topics = []
    seen = set()
    anchors = list(anchor_re.finditer(html))
    for i, m in enumerate(anchors):
        topic_id = m.group(1)
        if topic_id in seen:
            continue
        seen.add(topic_id)
        clean_title = _lsb_title(m.group(2))
        if not clean_title or len(clean_title) <= 2:
            continue
        row_end = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
        ts_match = time_re.search(html, m.end(), row_end)
        created_at = None
        try:
            if ts_match:
                created_at = datetime.fromtimestamp(
                    int(ts_match.group(1)), tz=timezone.utc
                ).isoformat()
        except Exception:
            pass
        topics.append({
            "id": topic_id,
            "title": clean_title,
            "url": f"{BASE_LINUXSB}/topic/{topic_id}",
            "created_at": created_at,
        })
    return topics


def fetch_linuxsb(known_ids: set | None = None) -> list:
    """Fetch linux.sb listings.

    known_ids: ids already stored with a trustworthy created_at. Detail pages
    are only fetched for ids NOT in this set, keeping incremental runs cheap.
    """
    known_ids = known_ids or set()
    sources = [
        ("linuxsb_福利放送", f"{BASE_LINUXSB}/forum/2?sort=post"),
        ("linuxsb_我要推广", f"{BASE_LINUXSB}/forum/8?sort=post"),
        ("linuxsb_抽奖", f"{BASE_LINUXSB}/index.php?sort=lucky"),
        ("linuxsb_发卡", f"{BASE_LINUXSB}/index.php?sort=card"),
        ("linuxsb_首页", f"{BASE_LINUXSB}/"),
    ]
    all_topics = []
    for name, url in sources:
        try:
            html = fetch(url)
            topics = parse_linuxsb_listing(html)
            for t in topics:
                t["source"] = name
            print(f"[fetch] {name}: {len(topics)} topics", file=sys.stderr)
            note_empty_source(name, len(topics))
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] {name} failed: {e}", file=sys.stderr)
            FETCH_ERRORS.append(f"linux.sb listing {name}: {e}")

    # data-performance-time is the last-modified time, not the post creation time.
    # Fetch detail pages only for topics we will actually keep: skip ids already
    # stored, and skip titles the relevance filter would drop anyway.
    def _relevant(t):
        return is_relevant_title(t.get("title", ""))

    need_detail = [
        t["id"] for t in all_topics
        if t["id"] not in known_ids and _relevant(t)
    ]
    skipped = len(all_topics) - len(need_detail)
    print(
        f"[fetch] linux.sb detail pages: {len(need_detail)} to fetch, "
        f"{skipped} skipped (cached or irrelevant)",
        file=sys.stderr,
    )

    published_map = {}
    if need_detail:
        with ThreadPoolExecutor(max_workers=6) as executor:
            future_to_id = {
                executor.submit(fetch_topic_published_time, tid): tid
                for tid in need_detail
            }
            for future in as_completed(future_to_id):
                tid = future_to_id[future]
                try:
                    published_map[tid] = future.result()
                except Exception:
                    pass

    # Drop topics we already have; they will be merged back from the store.
    all_topics = [t for t in all_topics if t["id"] not in known_ids]

    for t in all_topics:
        # created_at already holds the listing timestamp; overwrite it only with
        # the detail page's real publish time (published_verified gates the
        # skip-cached-ids optimisation, so an unverified row is re-checked next run).
        true_time = published_map.get(t["id"])
        if true_time:
            t["created_at"] = true_time
            t["published_verified"] = True

    return all_topics


def fetch_baipiao() -> list:
    all_topics = []
    for page in range(1, 4):
        api_url = f"{BASE_BAIPIAO}/bbs/api/discussions?page={page}&sort=-createdAt"
        html_url = f"{BASE_BAIPIAO}/bbs/all?page={page}"
        try:
            try:
                html = fetch(api_url)
                data = json.loads(html)
                topics = []
                for item in data.get("data", []):
                    attr = item.get("attributes", {})
                    topic_id = item.get("id", "")
                    slug = attr.get("slug", "")
                    title = attr.get("title", "")
                    created = attr.get("createdAt") or attr.get("lastPostedAt")
                    topics.append({
                        "id": str(topic_id),
                        "title": title,
                        "url": f"{BASE_BAIPIAO}/bbs/d/{slug}",
                        "created_at": created,
                        "source": f"baipiao_p{page}",
                        # baipiao's API createdAt IS the publish time.
                        "published_verified": True,
                    })
                # An API that answers 200 with an empty/changed payload is drift,
                # not an empty board: fall through to the HTML listing and let
                # note_empty_source() flag it if that is empty too.
                if not topics and page == 1:
                    raise ValueError("api returned 0 items")
            except Exception:
                html = fetch(html_url)
                topics = parse_topics(html, BaipiaoHTMLParser)
                for t in topics:
                    t["source"] = f"baipiao_p{page}"
            print(f"[fetch] baipiao page {page}: {len(topics)} topics", file=sys.stderr)
            if page == 1:
                note_empty_source("baipiao", len(topics))
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] baipiao page {page} failed: {e}", file=sys.stderr)
            FETCH_ERRORS.append(f"baipiao page {page}: {e}")
            break
    return all_topics


def fetch_nodeloc_welfare() -> list:
    """Fetch nodeloc.com welfare category (抽奖福利)."""
    all_topics = []
    for page in range(1, 4):
        api_url = f"{BASE_NODELOC}/c/welfare/12.json?order=created&page={page}"
        try:
            html = fetch(api_url)
            data = json.loads(html)
            topics = []
            for t in data.get("topic_list", {}).get("topics", []):
                topic_id = t.get("id")
                title = t.get("title", "")
                # Only created_at is a publish time; bumped_at/last_posted_at are
                # reply times, so a row dated by those must not claim a verified
                # publish time (that flag drives the skip-cached-ids optimisation).
                published = t.get("created_at")
                created = published or t.get("bumped_at") or t.get("last_posted_at")
                topics.append({
                    "id": str(topic_id),
                    "title": title,
                    "url": f"{BASE_NODELOC}/t/topic/{topic_id}",
                    "created_at": created,
                    "source": f"nodeloc_welfare_p{page}",
                    "published_verified": bool(published),
                })
            if not topics and page == 1:
                raise ValueError("api returned 0 items")
            print(f"[fetch] nodeloc welfare page {page}: {len(topics)} topics", file=sys.stderr)
            if page == 1:
                note_empty_source("nodeloc welfare", len(topics))
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] nodeloc welfare page {page} failed: {e}", file=sys.stderr)
            FETCH_ERRORS.append(f"nodeloc welfare page {page}: {e}")
            break
    return all_topics


def fetch_nodeloc() -> list:
    all_topics = []
    for page in range(1, 4):
        api_url = f"{BASE_NODELOC}/latest.json?order=created&page={page}"
        html_url = f"{BASE_NODELOC}/latest?order=created&page={page}"
        try:
            try:
                html = fetch(api_url)
                data = json.loads(html)
                topics = []
                for t in data.get("topic_list", {}).get("topics", []):
                    topic_id = t.get("id")
                    title = t.get("title", "")
                    # Prefer created_at; bumped_at/last_posted_at are reply times,
                    # which are usable as a fallback timestamp but are not the
                    # publish time.
                    published = t.get("created_at")
                    created = published or t.get("bumped_at") or t.get("last_posted_at")
                    topics.append({
                        "id": str(topic_id),
                        "title": title,
                        "url": f"{BASE_NODELOC}/t/topic/{topic_id}",
                        "created_at": created,
                        "source": f"nodeloc_p{page}",
                        "published_verified": bool(published),
                    })
                if not topics and page == 1:
                    raise ValueError("api returned 0 items")
            except Exception:
                html = fetch(html_url)
                topics = parse_topics(html, NodeLocHTMLParser)
                for t in topics:
                    t["source"] = f"nodeloc_p{page}"
            print(f"[fetch] nodeloc page {page}: {len(topics)} topics", file=sys.stderr)
            if page == 1:
                note_empty_source("nodeloc latest", len(topics))
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] nodeloc page {page} failed: {e}", file=sys.stderr)
            FETCH_ERRORS.append(f"nodeloc page {page}: {e}")
            break
    return all_topics


def fetch_linuxdo_welfare() -> list:
    """Fetch linux.do /c/welfare/36 via Scrapling StealthyFetcher.

    linux.do is behind Cloudflare; plain urllib gets a challenge page.
    Scrapling's StealthyFetcher uses patchright (stealth Playwright) to pass.

    Uses CSS selectors on the rendered page to extract topic links, then
    parses escaped JSON in the HTML for created_at timestamps.

    Set LINUXDO_ENABLED=0 to skip this source. GitHub-hosted runners sit on
    datacenter IPs that linux.do answers with 429, which would otherwise make
    every scheduled CI run red; the Windows task on a residential IP fetches it.
    """
    if os.environ.get("LINUXDO_ENABLED", "1") == "0":
        print("[info] linux.do: skipped (LINUXDO_ENABLED=0)", file=sys.stderr)
        return []

    try:
        from scrapling import StealthyFetcher
    except ImportError:
        print("[warn] linux.do: scrapling not installed, skipping", file=sys.stderr)
        FETCH_ERRORS.append("linux.do: scrapling not installed")
        return []

    all_topics = []

    for page_num in range(1, 3):
        url = f"{BASE_LINUXDO}/c/welfare/36.json"
        if page_num > 1:
            url += f"?page={page_num}"

        # One retry after a short pause: a 429 here is a transient rate limit,
        # not a refusal. Measured once the cadence went hourly -- 4 of 17 runs
        # got a 429 on page 2 and 1 on page 1, i.e. ~20% of runs silently lost
        # a whole page (the run still "succeeds" and merges what it has, so
        # nothing goes red). Fetching the same page again normally works, and
        # the retry cost is paid only by the runs that were losing data.
        raw = None
        for attempt in (1, 2):
            try:
                page = StealthyFetcher.fetch(url, headless=True, timeout=90000)
                body = page.body if isinstance(page.body, str) else page.body.decode("utf-8", "replace")
                if page.status == 429 and attempt == 1:
                    print(
                        f"[info] linux.do welfare page {page_num}: 429, retrying in 30s",
                        file=sys.stderr,
                    )
                    time.sleep(30)
                    continue
                if page.status != 200 or len(body) < 1000:
                    print(
                        f"[warn] linux.do welfare page {page_num}: "
                        f"status={page.status}, body_len={len(body)}",
                        file=sys.stderr,
                    )
                    FETCH_ERRORS.append(
                        f"linux.do welfare page {page_num}: status={page.status}"
                    )
                    break
                raw = body
                break
            except Exception as e:
                if attempt == 2:
                    print(
                        f"[warn] linux.do welfare page {page_num} failed: {e}",
                        file=sys.stderr,
                    )
                    FETCH_ERRORS.append(f"linux.do welfare page {page_num}: {e}")
                    break
        if raw is None:
            # Both attempts gone: stop instead of hammering the next page.
            break

        # Discourse JSON endpoint: topic_list.topics carries id/title/created_at
        # natively (verified 2026-10-02: 30 topics/page, every field populated).
        # NOTE: page.html_content wraps the payload in <html><body> — use page.body.
        data = json.loads(raw)
        topics = data.get("topic_list", {}).get("topics", [])
        # The first topic of the board is the pinned category description
        # ("关于福利羊毛类别", created 2024) — drop pinned/no-title entries.
        count = 0
        for topic in topics:
            topic_id = str(topic.get("id", ""))
            title = (topic.get("title") or "").strip()
            if not topic_id or not title or topic.get("pinned"):
                continue
            all_topics.append({
                "id": topic_id,
                "title": title,
                "url": f"{BASE_LINUXDO}/t/topic/{topic_id}",
                "created_at": topic.get("created_at"),
                "source": f"linuxdo_welfare_p{page_num}",
                # Discourse populates created_at for every topic; when it is
                # absent the row has no publish time at all, which is not a
                # verified one.
                "published_verified": bool(topic.get("created_at")),
            })
            count += 1

        print(f"[fetch] linux.do welfare page {page_num}: {count} topics", file=sys.stderr)
        if page_num == 1:
            note_empty_source("linux.do welfare", count)

    return all_topics


def fetch_vibex_welfare() -> list:
    """Fetch vibex.iflow.cn (心流AI社区) 补给站 - a Discourse board for AI 福利分享.

    Discourse JSON like nodeloc/linux.do, so this is the same three-field parse.
    Board is /c/4 ("iFlow 补给站"), verified 2026-10-04: 30 topics/page, plain
    urllib reaches it in ~0.7s with no proxy and no Cloudflare challenge, which
    matters because the GitHub runner has no proxy and sits on a datacenter IP.

    Deliberately only /c/4: the sibling boards are technical chatter, and
    measured against this project's own relevance keywords they yield ~0 hits
    (/c/12 AI探索舰 0/30, /c/14 AI摸鱼船 1/30) versus /c/4's 14/90.
    """
    all_topics = []
    for page_num in range(1, 4):
        api_url = f"{BASE_VIBEX}/c/4.json?order=created&page={page_num}"
        try:
            data = json.loads(fetch(api_url))
            topics = []
            for t in data.get("topic_list", {}).get("topics", []):
                topic_id = t.get("id")
                title = (t.get("title") or "").strip()
                # Discourse pins the board description as the first topic
                # ("关于...类别", years old) - it is not a welfare post.
                if not topic_id or not title or t.get("pinned"):
                    continue
                # created_at is the publish time; bumped_at/last_posted_at are
                # reply times and must not be presented as a verified one.
                published = t.get("created_at")
                created = published or t.get("bumped_at") or t.get("last_posted_at")
                topics.append({
                    "id": str(topic_id),
                    "title": title,
                    "url": f"{BASE_VIBEX}/t/topic/{topic_id}",
                    "created_at": created,
                    "source": f"vibex_welfare_p{page_num}",
                    "published_verified": bool(published),
                })
            if not topics and page_num == 1:
                raise ValueError("api returned 0 items")
            print(f"[fetch] vibex welfare page {page_num}: {len(topics)} topics", file=sys.stderr)
            if page_num == 1:
                note_empty_source("vibex welfare", len(topics))
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] vibex welfare page {page_num} failed: {e}", file=sys.stderr)
            FETCH_ERRORS.append(f"vibex welfare page {page_num}: {e}")
            break
    return all_topics

# ---------------------------------------------------------------------------
# NextBuf（NB 社区，www.nextbuf.com）
#
# 站点形态：V2EX-Modern 主题的 PHP 论坛，服务端渲染；没有可用的 JSON API
# （/api/* 回 500，/latest.json 之类软回落到首页 HTML）；robots.txt 只挡
# /admin、/install、/runcache，允许抓列表；首页列表混排全部板块，翻页是
# /page-N，所以只爬首页分页即可覆盖全站（约 200 条主题 / 9 页）。
#
# 关键差异：**列表里没有绝对时间**。列表与主题页给的都是相对时间（"17天前"、
# "31分钟前"、主题页的"发布于 1月前"），主题页也没有 article:published_time。
# 因此 created_at 只能是估算值，published_verified 一律保持 False。
# ---------------------------------------------------------------------------
NEXTBUF_PAGES = 3

# 列表条目：<a href="/t/160" class="topic-title-link " data-tid="160" style="">
#             <span class="topic-badge redpacket" title="回帖红包主题">红包</span> 0.1 倍率opus5</a>
_NEXTBUF_ITEM_RE = re.compile(
    r'<a[^>]*href="/t/(\d+)"[^>]*class="[^"]*topic-title-link[^"]*"[^>]*>(.*?)</a>', re.S
)
_NEXTBUF_BADGE_RE = re.compile(r'<span[^>]*class="[^"]*topic-badge[^"]*"[^>]*>.*?</span>', re.S)
_NEXTBUF_OPEN_TAG_RE = re.compile(r"<a[^>]*>", re.S)
_NEXTBUF_REL_RE = re.compile(r"(\d+)\s*个?\s*(分钟|小时|天|周|月|年)\s*前|刚刚")
_NEXTBUF_CST = timezone(timedelta(hours=8))


def nextbuf_title(anchor_inner: str) -> str:
    """锚点内文本剥掉徽章 span，得到真正的标题。

    列表把「红包」（回帖红包主题）和「置顶」做成 <span class="topic-badge ...">
    贴在标题前面。整段照抄会让「红包」混进标题 —— 闸门是按标题判的，多一个
    「红包」就可能把一条闲聊帖按平台内货币放进来（同「积分不是领域词」的道理）。
    """
    stripped = _NEXTBUF_BADGE_RE.sub(" ", anchor_inner)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", stripped)).strip()


def nextbuf_created_at(rel: str | None, now: datetime | None = None) -> str | None:
    """把列表里的相对时间换算成 ISO 时间戳，并按粒度归整（floor）。

    归整不是美化，是必须的：这个值会进 store 的 created_at，而 _ROW_FIELDS 里就
    有 created_at —— 相对时间随 now 漂移，不归整时同一条没变的帖每轮算出不同的
    值，「没有新闻的一轮」也会产生 commit 并触发一次 Pages 部署。归整到
    本小时 / 当天 00:00 / 当月 1 号之后，同一时间桶里重复抓到的值完全一致。

    换算刻意保守（1月=30天、1年=365天），粒度也只给到桶：这不是源站给的发布时间，
    所以调用方不设 published_verified（与 nodeloc 的 bumped_at 回落同理）。
    """
    if not rel:
        return None
    local = (now or datetime.now(timezone.utc)).astimezone(_NEXTBUF_CST)
    if "刚刚" in rel:
        n, unit = 0, "分钟"
    else:
        m = _NEXTBUF_REL_RE.search(rel)
        if not m:
            return None
        n = int(m.group(1) or 0)
        unit = m.group(2) or "分钟"
    if unit in ("分钟", "小时"):
        est = local - timedelta(hours=n if unit == "小时" else 0,
                                minutes=n if unit == "分钟" else 0)
        est = est.replace(minute=0, second=0, microsecond=0)               # → 本小时
    elif unit in ("天", "周"):
        est = local - timedelta(days=n * 7 if unit == "周" else n)
        est = est.replace(hour=0, minute=0, second=0, microsecond=0)       # → 当天 00:00
    else:
        est = local - timedelta(days=n * 30 if unit == "月" else n * 365)
        est = est.replace(day=1, hour=0, minute=0, second=0, microsecond=0)  # → 当月 1 号
    return est.astimezone(timezone.utc).isoformat()


def parse_nextbuf_listing(html: str, now: datetime | None = None) -> list:
    """One row per topic on a NextBuf listing page. 纯函数：不联网。

    时间取该条目自己的**发帖**相对时间（"1月前"），不是它后面那段「最后回复来自
    X」—— 后者是回复时间，当成发布时间会让老帖每被顶一次就变成新帖。所以相对
    时间的搜索范围在 meta-last-reply 处截断（同 linux.sb 那条「每行只认自己的
    时间戳」的教训：绝不让一行吃掉邻行的时间）。

    隐藏主题（style 里带 display:none）跳过：站点仍把它们渲染进 HTML。
    """
    matches = list(_NEXTBUF_ITEM_RE.finditer(html))
    topics = []
    seen = set()
    for i, m in enumerate(matches):
        topic_id, anchor_inner = m.group(1), m.group(2)
        if topic_id in seen:
            continue
        open_tag = _NEXTBUF_OPEN_TAG_RE.match(m.group(0))
        if open_tag and "display:none" in open_tag.group(0).replace(" ", ""):
            continue
        title = nextbuf_title(anchor_inner)
        if not title:
            continue
        seen.add(topic_id)
        row_end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        tail = html[m.end():row_end]
        cut = tail.find("meta-last-reply")
        rel = _NEXTBUF_REL_RE.search(tail if cut < 0 else tail[:cut])
        topics.append({
            "id": topic_id,
            "title": title,
            "url": f"{BASE_NEXTBUF}/t/{topic_id}",
            "created_at": nextbuf_created_at(rel.group(0) if rel else None, now=now),
            "published_verified": False,
        })
    return topics


def fetch_nextbuf(known_ids: set | None = None) -> list:
    """NextBuf 首页分页（全部板块混排）。

    known_ids：store 里已有的 id。相对时间只能给到桶粒度、重抓同一条帖也算出同一个
    值（见 nextbuf_created_at），所以已入库的直接跳过，不浪费请求；旧的 nextbuf 行
    由增量合并原样保留回 store。轮内新帖与失效帖仍会被看到。

    只爬前 NEXTBUF_PAGES 页：列表按最后回复排序，新帖必在第 1 页，3 页（约 90 条）
    覆盖最近有活动的主题，再往后翻只是重复老帖。
    """
    known_ids = known_ids or set()
    all_topics = []
    for page_num in range(1, NEXTBUF_PAGES + 1):
        url = f"{BASE_NEXTBUF}/" if page_num == 1 else f"{BASE_NEXTBUF}/page-{page_num}"
        try:
            rows = parse_nextbuf_listing(fetch(url))
        except Exception as e:
            print(f"[warn] nextbuf page {page_num} failed: {e}", file=sys.stderr)
            FETCH_ERRORS.append(f"nextbuf page {page_num}: {e}")
            break
        if page_num == 1:
            note_empty_source("nextbuf", len(rows))
        fresh = [t for t in rows if t["id"] not in known_ids]
        print(f"[fetch] nextbuf page {page_num}: {len(rows)} topics, {len(fresh)} new",
              file=sys.stderr)
        all_topics.extend(fresh)
        if not rows:
            break
    for t in all_topics:
        t["source"] = "nextbuf_首页"
    return all_topics


_ROW_FIELDS = ("title", "url", "source", "created_at")


def _row_changed(prev: dict, t: dict) -> bool:
    """True when a freshly fetched row differs from the stored one in anything
    the site shows.

    fetched_at is deliberately excluded: it means "when this row last changed",
    not "when we last looked at it". Keeping it stable for unchanged rows is what
    makes a run with no news produce a byte-identical store (and therefore no
    commit).
    """
    if any((t.get(f) or None) != (prev.get(f) or None) for f in _ROW_FIELDS):
        return True
    if list(t.get("tags") or []) != list(prev.get("tags") or []):
        return True
    if int(t.get("score") or 0) != int(prev.get("score") or 0):
        return True
    return bool(t.get("published_verified")) != bool(prev.get("published_verified"))


# 滚动窗口的条数上限。见 --limit 处的说明：闸门放宽后入库量翻倍，200 会让
# 内容偏旧的源被整体挤出（实测 vibex 归零），因此固定在 300。
DEFAULT_LIMIT = 300


def main():
    ap = argparse.ArgumentParser(description="Fetch welfare topics from multiple sources")
    ap.add_argument("--output", "-o", default="data/topics.jsonl")
    # cap 300 而不是 200：闸门放宽后每轮入库量翻倍（75 → 152），固定 200 会被
    # 新内容瞬间填满，滚动窗口从 49 天塌到 11 天，内容偏旧的一整个源（vibex，
    # 最新帖 2026-09-08）被 cap 全部挤出、静默归零。300 把窗口还原到 65 天，
    # 五个源都有代表。改这个值时请连带看 README 的「cap 与窗口跨度」一节，
    # 并跑 tests/test_parsers.py 里的 DEFAULT_LIMIT 断言。
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument(
        "--probe-missing",
        type=int,
        default=15,
        help="每轮最多探测多少条「已不在源列表里」的帖子；只有回 404/410 才判为已删除并移除，"
        "其余结果一律保留。0 = 关闭探测",
    )
    ap.add_argument(
        "--dump-candidates",
        default="",
        help="把**预过滤**语料（闸门之前解析出的全部帖子）写到这个文件，供离线规则 A/B。"
        "默认关闭；写进去的是候选全集，不写 store、不参与提交。",
    )
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    # Load existing topics for incremental merge
    # A damaged store must never be silently treated as "empty": the blackout
    # guard below keys off `existing`, so swallowing a parse failure would let
    # a partial fetch overwrite the whole merged history.
    existing = {}
    store_lines = 0
    store_bad = 0
    if os.path.exists(args.output):
        with open(args.output, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                store_lines += 1
                try:
                    t = json.loads(line)
                    existing[topic_key(t)] = t
                except Exception:
                    store_bad += 1
        print(f"[merge] loaded {len(existing)} existing topics", file=sys.stderr)
        if store_lines and not existing:
            print(
                f"[fatal] {args.output} has {store_lines} line(s) but none parsed "
                "- refusing to overwrite a damaged store",
                file=sys.stderr,
            )
            sys.exit(3)
        if store_bad:
            print(
                f"[warn] {store_bad}/{store_lines} store line(s) unparseable; "
                f"kept {len(existing)}",
                file=sys.stderr,
            )
            FETCH_ERRORS.append(
                f"store: {store_bad}/{store_lines} unparseable line(s) in {args.output}"
            )

    # linux.sb ids whose created_at is trustworthy (real publish time, not the
    # last-modified time from the listing). Only these may skip detail fetches.
    # Scoped to linuxsb_* sources: other sites use their own id namespaces.
    known_linuxsb_ids = {
        t["id"]
        for t in existing.values()
        if t.get("published_verified")
        and str(t.get("source", "")).startswith("linuxsb_")
    }

    # nextbuf 的 created_at 是相对时间估算（列表没有绝对时间），而 fetch_nextbuf
    # 对已入库的 id 直接跳过 —— 判据是 id 在 store 里就够，与 published_verified 无关。
    known_nextbuf_ids = {
        t["id"]
        for t in existing.values()
        if str(t.get("source", "")).startswith("nextbuf")
    }

    linuxsb_topics = fetch_linuxsb(known_linuxsb_ids)
    baipiao_topics = fetch_baipiao()
    nodeloc_topics = fetch_nodeloc()
    nodeloc_welfare_topics = fetch_nodeloc_welfare()
    linuxdo_topics = fetch_linuxdo_welfare()
    vibex_topics = fetch_vibex_welfare()
    nextbuf_topics = fetch_nextbuf(known_nextbuf_ids)
    all_topics = (linuxsb_topics + baipiao_topics + nodeloc_topics + nodeloc_welfare_topics
                  + linuxdo_topics + vibex_topics + nextbuf_topics)

    # Partial failures are survivable now that we merge incrementally: cached
    # rows for the failed source stay in the store. Warn loudly (the workflow
    # turns this into a red run) but still write.
    if FETCH_ERRORS:
        print(f"[warn] {len(FETCH_ERRORS)} fetch error(s); cached rows are kept:", file=sys.stderr)
        for err in FETCH_ERRORS:
            print(f"        - {err}", file=sys.stderr)
        # Marker so CI can surface a partial run as a red build *after* the
        # good data has been committed and pushed.
        status_path = os.path.join(os.path.dirname(args.output) or ".", ".fetch_errors")
        try:
            _atomic_write(status_path, "\n".join(FETCH_ERRORS) + "\n")
        except Exception:
            pass
    else:
        status_path = os.path.join(os.path.dirname(args.output) or ".", ".fetch_errors")
        if os.path.exists(status_path):
            os.remove(status_path)

    # Total blackout: nothing fetched at all AND we already have data. That is a
    # network/proxy outage, not an empty site. Refuse to write rather than risk
    # clobbering the store.
    if not all_topics and existing:
        print(
            "[fatal] every source returned 0 topics while the store has "
            f"{len(existing)} rows — refusing to write (network/proxy outage?)",
            file=sys.stderr,
        )
        sys.exit(2)

    # Deduplicate fetched topics
    all_topics = deduplicate(all_topics)

    # Score and tag
    all_topics = [score_topic(t) for t in all_topics]

    # Keep only welfare-relevant topics based on title keywords
    def is_relevant(t: dict) -> bool:
        return is_relevant_title(t.get("title", ""))

    # 删除判定要用「闸门过滤前」的抓取集合：一条帖可能只因闸门收紧而不再入选，
    # 那不等于源站删了它 —— 拿过滤后的集合比对，会把闸门的账算到源站头上。
    fetched_pre = all_topics
    fetched_keys = {topic_key(t) for t in fetched_pre}

    # 离线复盘用：把**预过滤**语料整份落盘。闸门/分类规则改动的 A/B 对拍全靠它 ——
    # 必须放在闸门**之前**，因为被丢掉的那些帖正是「漏抓」的唯一证据来源；
    # 只拿过闸结果没法评估漏抓。默认关闭，不写进 store、不参与提交。
    # 用法见 RULES.md 第 9 节（tests/ab_rules.py 就是拿这个文件做新旧版本对拍）。
    if args.dump_candidates:
        with open(args.dump_candidates, "w", encoding="utf-8") as f:
            for t in fetched_pre:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        print(
            f"[dump] {len(fetched_pre)} pre-filter candidates -> {args.dump_candidates}",
            file=sys.stderr,
        )

    before = len(fetched_pre)
    all_topics = [t for t in fetched_pre if is_relevant(t)]
    print(f"[filter] relevance filter: {before} -> {len(all_topics)} topics", file=sys.stderr)

    # Merge fetched topics with existing: new data overrides old for the same
    # key. Cross-site id collisions cannot happen here at all: the key carries
    # the host, so linux.sb#12345 and nodeloc.com#12345 are simply two rows.
    # (They used to be renamed to a qualified key at this point only, which the
    # store could not persist: on the next run the row loaded under its bare id,
    # looked brand new, and could be evicted as if the other site owned the id.)
    merged = dict(existing)
    url_changes = []
    for t in all_topics:
        key = topic_key(t)
        prev = merged.get(key)
        if prev is not None:
            if prev.get("url") and t.get("url") and prev["url"] != t["url"]:
                url_changes.append((key, prev["url"], t["url"]))
            # A source that stops emitting the publish time must not wipe one we
            # already know: created_at drives display order and the age cap, so
            # losing it drops the row to the front of the eviction queue.
            if not t.get("created_at") and prev.get("created_at"):
                t["created_at"] = prev["created_at"]
                t["published_verified"] = prev.get("published_verified", False)
        merged[key] = t
    for key, old_url, new_url in url_changes:
        print(f"[warn] url changed for {key}: {old_url} -> {new_url}", file=sys.stderr)

    # 已结束 / 已在源站消失的帖子不留到下一轮（用户 2026-10-04 决定）。
    #   (a) 标题带结束标记 —— 纯函数判定、不花请求，每轮都能清（实测积了 24 条）；
    #   (b) 源站这一轮看成了、这行却已不在列表里，且探到 404/410。
    # 源站失败的轮次绝不走 (b)：那时「不在列表里」只说明我们没看到，不说明帖子没了。
    stale_keys = [
        k for k, t in merged.items() if _STALE_RE.search((t.get("title") or "").lower())
    ]
    for k in stale_keys:
        merged.pop(k, None)
    if stale_keys:
        print(
            f"[purge] {len(stale_keys)} ended post(s) removed "
            "([已开奖] / 已完 / 已无 ...)",
            file=sys.stderr,
        )

    # 闸门是相关性的唯一权威，对 store 里的老行同样生效。
    # 只判新抓到的行会漏掉一整类：用旧规则放进来的噪音没有任何一轮会重判它，
    # 于是会一直留到被 cap 淘汰（实测 store 里积了 28 条，正是用户抱怨的那批）。
    # 30% 的闸门是防「闸门本身写错」的保险：真出回归时宁可这轮留着噪音，也不要
    # 一次删掉大半个库 —— 删掉的行若已沉出列表，就再也没有哪一轮抓得回来。
    # 另有 20 行的绝对下限：小库上「3 行里删 1 行」就是 33%，那不是回归。
    # 触发时记进 FETCH_ERRORS，CI 会变红、家宽侧会打 WARN，不会静默。
    if merged:
        gate_keys = [k for k, t in merged.items() if not is_relevant(t)]
        if len(merged) >= 20 and len(gate_keys) > len(merged) * 0.30:
            print(
                f"[purge] REFUSED to drop {len(gate_keys)}/{len(merged)} stored rows that "
                "fail the gate (>30%) - that looks like a gate regression, not noise. "
                "Nothing was removed; check the gate first.",
                file=sys.stderr,
            )
            FETCH_ERRORS.append("gate purge refused: >30% of the store fails the gate")
        elif gate_keys:
            for k in gate_keys:
                merged.pop(k, None)
            print(
                f"[purge] {len(gate_keys)} stored row(s) no longer pass the gate",
                file=sys.stderr,
            )

    # 打标签和闸门一样，是「标题」的纯函数 —— 每轮对整库重算。
    # 只在抓取时打分的话，旧行会永远保留旧规则的标签：昨天查库时「优惠渠道」只显示
    # 6 张卡，而按当前词表实际该有 32 条。分类变了不重算，用户看到的就是冻结的旧结果。
    relabeled = 0
    for t in merged.values():
        fresh = score_topic({"title": t.get("title") or ""})
        if list(t.get("tags") or []) != fresh["tags"] or int(t.get("score") or 0) != fresh["score"]:
            relabeled += 1
        t["tags"] = fresh["tags"]
        t["score"] = fresh["score"]
    if relabeled:
        print(f"[tags] re-tagged {relabeled} stored row(s) with the current rules", file=sys.stderr)

    if args.probe_missing > 0:
        bad_hosts = unhealthy_hosts(fetched_pre)
        candidates = [
            (k, t)
            for k, t in merged.items()
            if k not in fetched_keys
            and t.get("url")
            and host_of(t.get("source", "")) not in bad_hosts
        ]
        # 候选按「最新在前」排序，然后按小时轮转取一段固定窗口。
        # 不能只取前 N 条：那样永远只有最前面那几条被查，排在后面的候选一辈子
        # 探不到 —— 而沉底老帖恰恰是会被源站删掉的那类（它们本来就快被 cap 淘汰）。
        # 用「UTC 小时数 × 窗口大小 mod 候选数」当起点，不落任何状态文件，
        # 每轮换一批，约 len(candidates)/N 小时后整库扫完一遍。
        candidates.sort(
            key=lambda kv: _parse_sortable(kv[1].get("created_at"))
            or _parse_sortable(kv[1].get("fetched_at")),
            reverse=True,
        )
        step = args.probe_missing
        n = len(candidates)
        start = 0
        window = []
        if n:
            start = (int(datetime.now(timezone.utc).timestamp() // 3600) * step) % n
            window = [candidates[(start + i) % n] for i in range(min(step, n))]
        gone_keys = []
        for k, t in window:
            if probe_deleted(t["url"]):
                gone_keys.append(k)
        for k in gone_keys:
            merged.pop(k, None)
        print(
            f"[probe] checked {len(window)} of {n} off-list post(s) from sources seen "
            f"this run (window starts at #{start}); {len(gone_keys)} confirmed deleted "
            f"(404/410)",
            file=sys.stderr,
        )

    print(f"[merge] {len(existing)} existing + {len(all_topics)} fetched -> {len(merged)} total", file=sys.stderr)

    # Cap total: drop the OLDEST posts first (rolling window). A row with no
    # created_at falls back to fetched_at rather than to zero: zero means
    # "infinitely old", which evicted the newest linux.do rows first while rows
    # dated months ago survived. It also keeps the display order consistent with
    # what the card prints, since the card uses the same fallback.
    def age_key(t):
        return _parse_sortable(t.get("created_at")) or _parse_sortable(t.get("fetched_at"))

    # (key, topic) pairs: the key is the merge identity,
    # and it differs from the bare id only for a cross-site id collision.
    merged_items = list(merged.items())
    if len(merged_items) > args.limit:
        merged_items.sort(key=lambda kv: age_key(kv[1]), reverse=True)  # newest first
        dropped = merged_items[args.limit:]
        merged_items = merged_items[: args.limit]
        oldest = dropped[-1][1]  # the list is sorted newest-first
        print(
            f"[limit] trimmed {len(dropped)} oldest posts (cap {args.limit}); "
            f"oldest dropped id={oldest.get('id')} "
            f"at {oldest.get('created_at') or oldest.get('fetched_at')}",
            file=sys.stderr,
        )

    # Display order: score desc, then created_at desc, then id desc
    def sort_key(t):
        score = -t.get("score", 0)
        created = -age_key(t)
        id_num = -int(re.search(r"\d+", t["id"]).group(0)) if re.search(r"\d+", t["id"]) else 0
        return (score, created, id_num)

    merged_items.sort(key=lambda kv: sort_key(kv[1]))

    # Normalize output. fetched_at moves only for rows that actually changed, so
    # a run that finds nothing new leaves the store byte-identical and the
    # scheduled job has nothing to commit and no Pages deploy to trigger.
    now_iso = datetime.now(timezone.utc).isoformat()
    out = []
    updated = 0
    for key, t in merged_items:
        prev = existing.get(key)
        if prev is not None and not _row_changed(prev, t):
            fetched_at = prev.get("fetched_at") or now_iso
        else:
            fetched_at = now_iso
            updated += 1
        out.append({
            "id": t["id"],
            "title": t["title"],
            "url": t["url"],
            "tags": t.get("tags", []),
            "score": t.get("score", 0),
            "source": t.get("source", ""),
            "fetched_at": fetched_at,
            "created_at": t.get("created_at"),
            "published_verified": bool(t.get("published_verified")),
        })

    store_changed = _write_if_changed(
        args.output,
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in out),
    )

    print(
        f"[done] {len(out)} topics in {args.output}: {updated} updated, "
        f"{len(out) - updated} unchanged"
        + ("" if store_changed else " (store already up to date, nothing written)"),
        file=sys.stderr,
    )


def _parse_sortable(created_at):
    if not created_at:
        return 0
    try:
        s = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception:
        return 0


if __name__ == "__main__":
    main()
