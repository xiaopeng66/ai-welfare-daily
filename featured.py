"""精选项目：从 store + 正文缓存里筛"值得薅的鸡蛋"，维护 15 天上架周期。

纯规则、离线、**零 token**：额度 / 条件 / 入口 / 有效期全部正则抽取，摘要只从正文或标题里选句
（不改写、不编数字）。状态存 data/featured.jsonl（一行一条 = 整卡快照）——
即使帖子被 300 行 store 窗口挤出去，板上内容也照常显示到"上架 15 天 / 限时到期"为止。

门槛（用户 2026-10-16 口径）：
  · 赠送 / 抽奖：score >= MIN_SCORE(5.0)
  · 免费：必须有入口 + 发放语境 + 说得清"能拿到什么" + (正文>=150 字 或 >=60 字且命中强发放词)
  · 需充值才拿到的不算鸡蛋；"几十家合集"帖里金额属于别家站点，不当卡
产品口径：
  · 时间窗最近 3 天；活动身份用 offer.campaign（v8 正文层专为这个板块加的）
  · 一个活动一张卡（冠军帖）：卡上的额度/条件/摘要/入口都取自同一条帖子
  · 按最新时间排序；限时活动到点自动下架，其余上架 15 天到期下架
  · 同活动后来发了新帖 → 换最新那条当原帖、重新总结，但上架日不重置
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
WINDOW_DAYS = 3
LIFE_DAYS = 15
MAX_ITEMS = 300
STATE_KEYS = ("t", "listed_iso", "listed_sim")   # 只写状态文件、不进页面


def load_topics(path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_cache(cache_dir) -> dict:
    out = {}
    for name in os.listdir(cache_dir):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(cache_dir, name), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("url"):
            out[d["url"]] = d
    return out


def load_state(path) -> dict:
    try:
        rows = load_topics(path)
    except OSError:
        return {}
    return {e["key"]: e for e in rows if isinstance(e, dict) and e.get("key")}


def save_state(path, board: list[dict]) -> None:
    """整文件原子重写：条目几十条，重写比按行改简单可靠。"""
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for item in board:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def update(rows, cache_or_dir, state_path, now: datetime | None = None):
    """跑一轮：返回 (页面用 payload, 统计)。调用方要自己 try/except（主站页面不能被它拖停）。"""
    now = now or datetime.now(CST)
    cache = cache_or_dir if isinstance(cache_or_dir, dict) else load_cache(cache_or_dir)
    fresh = build_items(rows, cache, now)
    board, stats = merge_board(load_state(state_path), fresh, now, PASSED)
    save_state(state_path, board)
    payload = [{k: v for k, v in it.items() if k not in STATE_KEYS} for it in board]
    return payload, stats


CST = timezone(timedelta(hours=8))
REPO = "E:/AI/Hermes/scripts/linuxsb-daily"
OUT = "E:/AI/Zcode/tmp/featured-draft-20261016"
WINDOW_DAYS = 3
LIFE_DAYS = 15
MAX_ITEMS = 300          # 安全上限；真正的边界是"15 天"和"限时到期"两条规则
STATE = OUT + "/featured-state.json"   # 精选板自己的状态（正式形态：data/featured.jsonl）
MIN_SCORE = 5.0            # 赠送/抽奖类门槛（用户 2026-10-16：调高一点）
CNY_PER_USD = 7.0

CARRIER_HOSTS = (
    "t.co", "bit.ly", "github.com", "gitlab.com", "gitee.com", "pan.baidu.com", "lanzou", "drive.google",
    "docs.google", "apps.apple.com", "play.google.com", "x.com", "twitter.com", "bilibili.com",
    "nodeseek.com", "v2ex.com", "hostloc.com", "linux.do", "linux.sb", "baipiao.org", "t.me", "telegram",
    "qq.com", "weixin", "youtube.com", "esimplus.me", "cdk.linux.do",
    "nodeloc.com", "nextbuf.com", "w2solo.com", "v2ex.co",
)

# 活动别名表：与 content.py 的 CAMPAIGN_ALIASES 对齐（一个活动多个入口，卡片按活动聚合）
CAMPAIGN_ALIASES = {
    "muse": ("muse.ai", "t.me/satudigitalbot"),
}


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1) if m else "").lower()


def when(r: dict) -> datetime | None:
    for k in ("created_at", "fetched_at"):
        v = r.get(k)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(CST)
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------- 额度（只在送礼语境里算数）
GIFT_WORD = re.compile(r"送|赠|领|得|返|注册|试用|免费|白嫖|名额|先到|最高|合计|到手|补贴|福利|限时|回复后可见|兑换码|兑换|换|口令|号池|key|丢|蹬|放出|白送|无偿|鸡蛋|补偿")
AMOUNT_PATTERNS = (
    (re.compile(r"(?:[$￥¥])\s?(\d+(?:\.\d+)?)"), "usd"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[$￥¥]"), "usd"),          # 2$ / 2￥（数字在前，实测常写）
    (re.compile(r"(\d+(?:\.\d+)?)\s*[wW]\s*(?:token|词元|额度)"), "wan_token"),   # 3000wtoken
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:刀|美刀|美元|usd)", re.I), "usd"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*元"), "cny"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*亿"), "token"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*万\s*(?:token|词元|额度)"), "wan_token"),
)
LINE_SPLIT = re.compile(r"[\n\r]+|(?<=[。！!？?;；])\s*")
# 价格/门槛语境：数字附近出现这些词说明它是售价或消费门槛，不是发放额度
PRICE_CTX = re.compile(r"起|折|倍率|门槛|满|消费|付款|付费|购买|套餐|会员|订阅|价格")


def _band_usd(v: float) -> float:
    for cut, score in ((200, 6), (100, 5), (50, 4.5), (20, 4), (10, 3), (5, 2), (2, 1)):
        if v >= cut:
            return float(score)
    return 0.0


def amount_of(text: str) -> tuple[float, str, str]:
    """(排序分, 展示文本, 单位)。额度必须出现在"发放语境"±40 字内，且展示一律照抄原文。

    为什么用窗口而不是"同一行"：真实帖子常把量值和发放词拆到相邻行
    （实测 lunarfox：「666刀」/「纯heavy号池」/「限时12个小时」各占一行），
    按行判会整条漏掉；而广告（gt.tc「低至 ¥0.0211/刀起」）在 ±40 字窗口里
    没有任何发放词，仍然拦得住。
    """
    t = CLEAN_URL.sub(" ", text or "")
    best = (0.0, "", "")
    for pat, unit in AMOUNT_PATTERNS:
        for m in pat.finditer(t):
            # 发放词必须**紧贴**数字（±10 字）：±40 那种宽窗口会把奖励表里的
            # "消费 $200 固定返 $10" 当成"送 $200"（nexotoken 实测踩过）。
            window = t[max(0, m.start() - 10):m.end() + 10]
            if not GIFT_WORD.search(window):
                continue
            if PRICE_CTX.search(t[max(0, m.start() - 6):m.end() + 6]):
                continue                                     # 售价 / 消费门槛，不是发放额度
            v = float(m.group(1))
            if unit == "usd":
                score = _band_usd(v)
            elif unit == "cny":
                score = _band_usd(v / CNY_PER_USD)
            elif unit == "token":
                score = _band_usd(v / 10.0) if v >= 10 else (1.5 if v >= 3 else 1.0)
            else:  # 万 token → 折到亿
                score = _band_usd(v / 100000.0)
            if score > best[0]:
                best = (score, m.group(0).strip(), unit)
    return best


# ---------------------------------------------------------------- 条件（否定前缀 + 剥 URL）
CLEAN_URL = re.compile(r"https?://\S+")
NEG = re.compile(r"(?:不用|无需|不需要|不必|免|无需再|不须)")
STEPS = (
    ("需首充", re.compile(r"首充|充值后|充值才|充值解锁|需要充值|需充值|充值满|累计充值|充值达标|购买套餐|付费解锁|付费才能|开通会员|先充")),
    ("需运气", re.compile(r"抽奖|随机|拼手气|概率|大奖|中奖")),
    ("需邀请码", re.compile(r"邀请码|邀请链接|invite")),
    ("需回复", re.compile(r"回帖|回复.{0,2}可见|评论|盖楼")),
    ("需拉人", re.compile(r"拉新|拉人|邀请好友|推广给")),
    ("需签到", re.compile(r"签到|每日打卡")),
    ("需手机号", re.compile(r"手机号")),
    ("需邮箱", re.compile(r"邮箱")),
    ("需绑TG", re.compile(r"绑定\s*(?:telegram|tg)|telegram\s*绑定")),
    ("需外网", re.compile(r"需要魔法|科学上网|需开梯子|开🪜")),
    ("限量", re.compile(r"限量|名额有限|先到先得|仅限")),
    ("限时", re.compile(r"限时|限\s?\d+\s?(?:个)?小时")),
)


def steps_of(text: str) -> tuple[list[str], bool]:
    """命中 + 否定过滤：只有"命中点前 4 字内没有否定词"才算一条门槛。

    为什么不能在整段上预剥否定词：`免` 本身是否定词缀，预剥会把「**免费**」吃成「费」，
    于是免费福利全被判成"没有免费证据"（v3 实测：bolt.new / llm.pm 因此被漏掉）。
    """
    t = CLEAN_URL.sub(" ", text or "")
    tags = []
    for label, pat in STEPS:
        for m in pat.finditer(t):
            prefix = t[max(0, m.start() - 4):m.start()]
            if not re.search(NEG.pattern + r"\s*$", prefix):
                tags.append(label)
                break
    free = bool(re.search(r"免费|白嫖|零门槛|注册即送|注册送|注册得|0元|免费试用", t))
    return tags, free


def deadline_of(text: str, posted: datetime) -> tuple[str, float | None]:
    """从正文里解出「截止时刻」→ (标签, epoch 秒 | None)。

    锚点按用户口径 = **帖子发布时间**（限时窗口从发帖那一刻开始算，不是我们发现它的时候）。
    只认能确定的写法，不确定就不倒计时（编一个时间出来就是撒谎）：
      ① 限时 N 小时 / N 天           → 发帖时间 + N
      ② 「截止/结束/开奖」+ 显式日期时间 → 直接取（日期没写钟点时按当天 23:59）
      ③ 只有"限时"两个字、或只有"开始时间" → 不倒计时
    为什么关键词必须紧贴日期（≤20 字）：实测 bolt.new 的正文里"开始时间…10月17日 15:00
    结束时间…10月19日"两个日期挨着，光看"时间内出现时间"会把**开始**时间当截止。
    """
    t = CLEAN_URL.sub(" ", text or "")
    # "限时12个小时" / "限时开放注册 1 小时" / "限时 N 天"：中间允许任意非数字字符（含空格）
    m = re.search(r"限时[^\d]{0,12}?(\d+)\s*(?:个)?\s*小时", t)
    if m:
        return "领取截止", (posted + timedelta(hours=int(m.group(1)))).timestamp()
    m = re.search(r"限时[^\d]{0,12}?(\d+)\s*天", t)
    if m:
        return "领取截止", (posted + timedelta(days=int(m.group(1)))).timestamp()
    # "1 小时内有效 / 2 天内截止"（不带"限时"两字也认）
    m = re.search(r"(\d+)\s*(?:个)?\s*小时[^\d]{0,4}?(?:内有效|内领取|内截止)", t)
    if m:
        return "领取截止", (posted + timedelta(hours=int(m.group(1)))).timestamp()
    m = re.search(r"(\d+)\s*(?:个)?\s*天[^\d]{0,4}?(?:内有效|内截止|后截止)", t)
    if m:
        return "领取截止", (posted + timedelta(days=int(m.group(1)))).timestamp()
    # "活动截止时间为15号 / 15日截止"（只有日号：按发帖当月算，已过则顺延一个月）
    m = re.search(r"截止[^\d]{0,10}?(\d{1,2})\s*(?:号|日)", t)
    if m:
        dom = int(m.group(1))
        year, month = posted.year, posted.month
        if dom < posted.day:
            month += 1
            if month > 12:
                month, year = 1, year + 1
        return "截止", datetime(year, month, dom, 23, 59, tzinfo=CST).timestamp()

    # 显式日期时间：日期（可带星期/下午）+ 可选钟点
    date_expr = (r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:[（(][^）)]{0,10}[）)]\s*)?"
                 r"(?:[上下]午\s*|晚上\s*)?(?:(\d{1,2})\s*[:：]\s*(\d{2}))?")
    key = re.compile(r"开奖|截止|结束")

    def resolve(month: int, dom: int, hh: str, mm: str) -> float:
        year = posted.year
        if (month, dom) < (posted.month, posted.day):      # 跨年（12 月发帖写 1 月）
            year += 1
        hour, minute = (int(hh), int(mm)) if hh else (23, 59)
        return datetime(year, month, dom, hour, minute, tzinfo=CST).timestamp()

    for pat in (re.compile(r"(?:开奖|截止|结束)[^。\n]{0,20}?" + date_expr),
                re.compile(date_expr + r"\s*(?:前)?\s*(?:开奖|截止|结束)")):
        m = pat.search(t)
        if m:
            month, dom, hh, mm = m.group(1), m.group(2), m.group(3), m.group(4)
            label = "开奖" if "开奖" in m.group(0) else "截止"
            return label, resolve(int(month), int(dom), hh, mm)

    # 「今天/明天 HH:MM 截止」这种相对日
    m = re.search(r"(今|明)天\s*(?:[上下]午\s*|晚上\s*)?(\d{1,2})\s*[:：]\s*(\d{2})\s*(?:前)?\s*(?:开奖|截止|结束)", t)
    if m:
        day = posted.date() + (timedelta(days=1) if m.group(1) == "明" else timedelta())
        dt = datetime(day.year, day.month, day.day, int(m.group(2)), int(m.group(3)), tzinfo=CST)
        return ("开奖" if "开奖" in m.group(0) else "截止"), dt.timestamp()

    if key.search(t):
        return "开奖", None
    return "", None


def expiry_of(text: str) -> str:
    t = CLEAN_URL.sub(" ", text or "")
    m = re.search(r"限时\s*(\d+)\s*(?:个)?小时", t)
    if m:
        return f"限时 {m.group(1)} 小时"
    m = re.search(r"(?:有效期|时长|可用|限)\s*(\d+)\s*天", t)
    if m:
        return f"有效期 {m.group(1)} 天"
    if re.search(r"限时开放|限时", t):
        return "限时"
    if re.search(r"免费试用", t):
        return "免费试用"
    if re.search(r"每周", t):
        return "每周活动"
    if re.search(r"每日|每天", t):
        return "每日可领"
    return ""


KEY_HINT = re.compile(r"送|赠|免费|额度|刀|亿|token|注册|邀请|有效期|天|次|抽|每天|每周|首充|领")


def clean_lines(body: str) -> list[str]:
    text = CLEAN_URL.sub(" ", body or "").replace("\u200b", " ")
    out = []
    for p in LINE_SPLIT.split(text):
        p = re.sub(r"\s+", " ", p).strip(" ·|-—…。")
        if len(p) >= 6 and re.search(r"[\u4e00-\u9fffA-Za-z]", p):
            out.append(p)
    return out


DOMAIN_RE = re.compile(r"[a-z0-9-]+\.(?:com|cn|net|ai|io|eu|cc|top|xyz|vip|org|me|dev|pro|surf|tech)", re.I)


def _bare_domain(line: str) -> bool:
    """整行就是个裸域名（或域名+时间戳之类），没有信息量，不该当摘要。"""
    stripped = (line or "").strip().strip("·|-—…。:：")
    return bool(stripped) and len(stripped) < 24 and bool(DOMAIN_RE.fullmatch(stripped))


def _foreign_host(line: str, entry: str) -> bool:
    """这一行提到了别家域名吗（与卡片入口不同的站点）。

    两个不算"别家"的情况：邮箱地址（undyingapi@gmail.com 里的域名）、
    同品牌不同后缀（logicc.ai 与 logicc.com 是同一家）。
    """
    text = re.sub(r"[\w.+-]+@[\w.-]+", " ", line or "")
    entry_host = (entry or "").split("/")[0].lower()
    entry_label = entry_host.split(".")[0]
    for host in DOMAIN_RE.findall(text):
        host = host.lower()
        if not entry_host:
            return True
        if entry_host not in host and host not in entry_host and host.split(".")[0] != entry_label:
            return True
    return False


def summarize(body: str, title: str, entry: str = "") -> tuple[list[str], str]:
    lines = clean_lines(body)
    if not lines:
        return ([title.strip()] if title else []), "title"
    # 提到别家域名的行降权：宁可换一句，也别让摘要里的站跟卡片标题对不上
    lines = [ln for ln in lines if not _bare_domain(ln)]
    if not lines:
        return ([title.strip()] if title else []), "title"
    ranked = sorted(range(len(lines)),
                    key=lambda i: (-len(KEY_HINT.findall(lines[i])) - (3 if _foreign_host(lines[i], entry) else 0),
                                   len(lines[i])))
    out, total = [], 0
    for i in sorted(ranked[:2]):
        s = lines[i]
        if total + len(s) > 150 and out:
            break
        out.append(s)
        total += len(s)
    return out[:2], "body"


# ---------------------------------------------------------------- 选品（冠军帖）
def champion_of(members: list[dict], cache: dict) -> dict | None:
    """组里的「冠军帖」：卡上一切数据（额度/条件/摘要/入口）都取自同一条帖子。

    优先级：不是"需充值"的 → 有发放语境的 → 额度分高 → 正文长 → 新。
    为什么先排除硬门槛帖：同一站点常同时有"送"和"打折"两类帖（gateai 组里既有
    $10 送礼帖、也有别口径的帖），按额度最大值取会取到优惠帖、把整张卡带偏（v1/v2 实测）。
    """
    best, best_key = None, None
    for m in members:
        body = (cache.get(m["url"]) or {}).get("body") or ""
        text = f"{m.get('title','')}\n{body}"
        score, _, _ = amount_of(text)
        tags, _free = steps_of(text)
        links = (cache.get(m["url"]) or {}).get("meta", {}).get("links") or []
        aggregator = len(links) >= 12 or body.count("http") >= 6
        gift = bool(GIFT_WORD.search(CLEAN_URL.sub(" ", text)))
        key = (0 if aggregator else 1, 0 if "需首充" in tags else 1, 1 if gift else 0, score,
               (when(m) or datetime.min.replace(tzinfo=CST)).timestamp(), len(body))
        if best_key is None or key > best_key:
            best, best_key = m, key
    return best


def norm_link(u: str) -> str:
    """把链接化成 host+path（小写、去尾斜杠），用于和活动身份比对。"""
    m = re.match(r"https?://([^?#]+)", u or "")
    return (m.group(1) if m else "").lower().rstrip("/")


def pick_entry(key: str, site: str, links: list[str]) -> str:
    """入口必须和卡片标题（活动身份）对得上 —— 对不上就不显示，不拿无关域名凑数。

    身份候选 = 活动别名表的成员 → offer.site → 活动 key（含 t.me/xxx 这种）。
    匹配顺序：链接的 host+path 命中候选 → 候选本身看起来是站点（含点）就直接用。
    """
    cands = list(CAMPAIGN_ALIASES.get(key, ())) + [c for c in (site, key) if c]
    norm = [norm_link("https://" + c) if not c.startswith("http") else norm_link(c) for c in cands]
    hosts = [norm_link(u) for u in links]
    for c, want in zip(cands, norm):
        for h in hosts:
            if h and want and (h == want or h.startswith(want + "/") or h.endswith("." + want.split("/")[0])):
                return c                     # 回身份本身（=卡片标题那份写法），保证标题与入口对得上
    for c in cands:                      # 没有可核对的链接时，用身份本身（只认"像域名"的）
        if "." in c.split("/")[0]:
            return c
    return ""


PASSED: dict[str, bool] = {}      # 本轮里每个活动键是否过门槛（供旧条目重判用）


def build_items(rows: list[dict], cache: dict, now: datetime) -> list[dict]:
    cut = now - timedelta(days=WINDOW_DAYS)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        t = when(r)
        if not t or t < cut:
            continue
        o = r.get("offer") or {}
        key = o.get("campaign") or o.get("key")
        if key:
            groups.setdefault(key, []).append(r)

    items = []
    for key, members in groups.items():
        members.sort(key=lambda r: when(r) or now, reverse=True)
        champ = champion_of(members, cache)
        if champ is None:
            continue
        champ_links = (cache.get(champ["url"]) or {}).get("meta", {}).get("links") or []
        champ_body = (cache.get(champ["url"]) or {}).get("body") or ""
        if len(champ_links) >= 12 or champ_body.count("http") >= 6:
            continue                                   # 汇总/合集帖：额度属于别家站点，不当一张卡
        body = (cache.get(champ["url"]) or {}).get("body") or ""
        links = list((cache.get(champ["url"]) or {}).get("meta", {}).get("links") or [])
        text = f"{champ.get('title','')}\n{body}"
        score, amount, unit = amount_of(text)
        tags, free = steps_of(text)
        expiry = expiry_of(text)
        entry_pre = pick_entry(key, (champ.get("offer") or {}).get("site") or "", links)
        lines, src = summarize(body, champ.get("title", ""), entry_pre)
        cd_label, cd_deadline = deadline_of(text, champ_time_for_cd := (when(champ) or now))
        entry = entry_pre
        gift_evidence = bool(GIFT_WORD.search(CLEAN_URL.sub(" ", text)))
        # 免费类的专门门槛（用户：尤其免费福利要仔细甄别）：说得清"能拿到什么"才算
        # "具体可得"词表：模型名 + 图像/视频/生图 + 额度/次数/接口 这类能核对的实物
        concrete = bool(re.search(r"claude|gpt|gemini|grok|deepseek|glm|qwen|kimi|llama|opus|sonnet|haiku"
                                  r"|token|词元|额度|次数|分组|倍率|签到|试用|积分|兑换"
                                  r"|图像|视频|生图|画图|模型|接口|api|key|号池|每日|每周", text, re.I))
        strong_free = bool(re.search(r"免费试用|限时免费|白嫖|0\s*倍率|0倍率|免费使用|免费领|注册即送|无需付费", text))
        hard = "需首充" in tags
        lucky = "需运气" in tags
        if hard:
            continue                                   # 需充值才能拿的不算"鸡蛋"（那是优惠渠道，另有所属）
        if not gift_evidence:                          # 正文/标题里没有任何发放语境 → 广告或倍率表
            continue
        if not amount and not free:                    # 连"免费"都没写、又没数字 → 站点推广，不当鸡蛋
            continue

        title_lot = bool(re.search(r"抽奖|开奖|抽码", str(champ.get("title") or "")))
        kind_pre = ("lottery" if ((amount and lucky) or cd_label == "开奖" or title_lot)
                    else "gift" if amount else "free")
        ease = 2.0 if (free and not hard) else (1.0 if not hard else -3.0)
        ease -= 0.5 * len([t for t in tags if t in ("需回复", "需邀请码", "需拉人", "需签到", "需外网", "限量")])
        ease -= 0.5 * len([t for t in tags if t in ("需邮箱", "需手机号", "需绑TG")])
        if lucky and kind_pre == "lottery":
            # 免费参与的抽奖只轻打；需要先充值/消费才抽的重罚（原来看齐 -1.5 误伤了免费抽奖）
            ease -= 1.5 if hard else 0.5
        ease = max(-4.0, ease)
        evidence = (1.0 if len(body) >= 200 else 0.0) + (1.0 if len(body) >= 600 else 0.0) + (1.0 if entry else 0.0)
        if len(body) < 40:
            evidence -= 2.0
        if len(links) > 10:                             # 汇总帖（几十个站点的 aff 列表）
            evidence -= 2.5
        recency = 0.5 if (when(champ) or now) >= now - timedelta(days=1) else 0.0
        total = score + ease + evidence + recency
        kind_pre = ("lottery" if ((amount and lucky) or cd_label == "开奖") else "gift" if amount else "free")
        if kind_pre == "free":
            # 免费类：必须有入口、有发放语境，且"具体可得"（正文≥150 字，或 ≥60 字 + 强发放词）
            rich = len(body) >= 150 or (len(body) >= 60 and strong_free)
            if not (entry and gift_evidence and concrete and rich):
                PASSED[key] = False
                continue
        elif total < MIN_SCORE:
            PASSED[key] = False
            continue
        PASSED[key] = True
        champ_time = when(champ) or now
        left = LIFE_DAYS - (now.date() - champ_time.date()).days
        if left <= 0:
            continue
        if cd_deadline and cd_deadline <= now.timestamp():
            continue                                   # 限时已过 → 到点下架（用户口径）
        item = {
            "key": key,
            "brand": (champ.get("offer") or {}).get("brand") or "",
            "amount": ("抽奖 " + amount if (amount and lucky) else amount)
                       or ("抽奖" if ((amount and lucky) or cd_label == "开奖" or title_lot) else
                           "限时免费" if "限时" in expiry else "免费试用" if "试用" in expiry else "免费福利"),
            "kind": ("lottery" if ((amount and lucky) or cd_label == "开奖") else "gift" if amount else "free"),
            "unit": unit,
            "score": round(total, 1),
            "level": ("需充值" if hard else "需运气" if lucky else
                      "零门槛" if (free and not [t for t in tags if t in ("需回复", "需邀请码", "需签到", "需拉人", "需手机号", "需绑TG", "需外网")])
                      else "低门槛"),
            "level_class": ("hard" if hard else "luck" if lucky else
                            "free" if (free and not [t for t in tags if t in ("需回复", "需邀请码", "需签到", "需拉人", "需手机号", "需绑TG", "需外网")])
                            else "mid"),
            "reqs": " · ".join(tags[:4]),
            "expiry": expiry,
            "cd_label": cd_label,
            "deadline": cd_deadline,
            "lines": lines,
            "summary_src": src,
            "entry": entry,
            "url": champ.get("url", ""),
            "title": champ.get("title", ""),
            "source": champ.get("source", ""),
            "tags": list(champ.get("tags") or []),
            "when": champ_time.strftime("%m-%d %H:%M"),
            "t": champ_time.timestamp(),
            "n": len(members),
            "on": now.strftime("%m-%d"),            # 上架日 = 进板当天（跟 listed_iso 同一口径）
            "left": LIFE_DAYS,
            "listed_iso": now.date().isoformat(),   # 上架日 = 进板当天（用户口径：上架 15 天）
            "listed_sim": champ_time.date().isoformat(),   # 草稿里按"帖子当天就上架"模拟，正式形态用 listed_iso
            "dups": len(members),
            "dupwhy": "offer",
            "dupkey": "F:" + key,
            "dupsList": [
                {"t": m.get("title", ""), "u": m.get("url", ""), "at": m.get("created_at") or m.get("fetched_at") or ""}
                for m in members
            ],
        }
        # 整卡快照（页面直接用这份渲染；状态文件里也存它 —— 这样 store 把帖子挤出去也不影响显示）
        item["f"] = {
            "name": key, "brand": item["brand"], "amount": item["amount"], "level": item["level"],
            "level_class": item["level_class"], "reqs": item["reqs"], "expiry": item["expiry"],
            "lines": item["lines"], "entry": item["entry"], "when": item["when"], "on": item["on"],
            "cd_label": item["cd_label"], "deadline": item["deadline"], "kind": item["kind"],
            "amt_pre": "抽奖" if str(item["amount"]).startswith("抽奖 ") else "",
            "amt_main": str(item["amount"])[3:] if str(item["amount"]).startswith("抽奖 ") else item["amount"],
            "left": item["left"], "n": item["n"], "src": item["summary_src"],
            # 证据快照：帖子出了 3 天窗口后，靠它复核（否则只能干等到 15 天）
            "ev": {"body": len(body), "concrete": concrete, "strong_free": strong_free, "entry": entry},
        }
        items.append(item)
    items.sort(key=lambda x: -x["t"])
    return items[:MAX_ITEMS] if len(items) > MAX_ITEMS else items


PASSED: dict[str, bool] = {}      # 本轮里每个活动键是否过门槛（供旧条目重判用）


def build_items(rows: list[dict], cache: dict, now: datetime) -> list[dict]:
    cut = now - timedelta(days=WINDOW_DAYS)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        t = when(r)
        if not t or t < cut:
            continue
        o = r.get("offer") or {}
        key = o.get("campaign") or o.get("key")
        if key:
            groups.setdefault(key, []).append(r)

    items = []
    for key, members in groups.items():
        members.sort(key=lambda r: when(r) or now, reverse=True)
        champ = champion_of(members, cache)
        if champ is None:
            continue
        champ_links = (cache.get(champ["url"]) or {}).get("meta", {}).get("links") or []
        champ_body = (cache.get(champ["url"]) or {}).get("body") or ""
        if len(champ_links) >= 12 or champ_body.count("http") >= 6:
            continue                                   # 汇总/合集帖：额度属于别家站点，不当一张卡
        body = (cache.get(champ["url"]) or {}).get("body") or ""
        links = list((cache.get(champ["url"]) or {}).get("meta", {}).get("links") or [])
        text = f"{champ.get('title','')}\n{body}"
        score, amount, unit = amount_of(text)
        tags, free = steps_of(text)
        expiry = expiry_of(text)
        lines, src = summarize(body, champ.get("title", ""))
        cd_label, cd_deadline = deadline_of(text, champ_time_for_cd := (when(champ) or now))
        entry = pick_entry(key, (champ.get("offer") or {}).get("site") or "", links)
        gift_evidence = bool(GIFT_WORD.search(CLEAN_URL.sub(" ", text)))
        # 免费类的专门门槛（用户：尤其免费福利要仔细甄别）：说得清"能拿到什么"才算
        # "具体可得"词表：模型名 + 图像/视频/生图 + 额度/次数/接口 这类能核对的实物
        concrete = bool(re.search(r"claude|gpt|gemini|grok|deepseek|glm|qwen|kimi|llama|opus|sonnet|haiku"
                                  r"|token|词元|额度|次数|分组|倍率|签到|试用|积分|兑换"
                                  r"|图像|视频|生图|画图|模型|接口|api|key|号池|每日|每周", text, re.I))
        strong_free = bool(re.search(r"免费试用|限时免费|白嫖|0\s*倍率|0倍率|免费使用|免费领|注册即送|无需付费", text))
        hard = "需首充" in tags
        lucky = "需运气" in tags
        if hard:
            continue                                   # 需充值才能拿的不算"鸡蛋"（那是优惠渠道，另有所属）
        if not gift_evidence:                          # 正文/标题里没有任何发放语境 → 广告或倍率表
            continue
        if not amount and not free:                    # 连"免费"都没写、又没数字 → 站点推广，不当鸡蛋
            continue

        kind_pre = ("lottery" if ((amount and lucky) or cd_label == "开奖") else "gift" if amount else "free")
        ease = 2.0 if (free and not hard) else (1.0 if not hard else -3.0)
        ease -= 0.5 * len([t for t in tags if t in ("需回复", "需邀请码", "需拉人", "需签到", "需外网", "限量")])
        ease -= 0.5 * len([t for t in tags if t in ("需邮箱", "需手机号", "需绑TG")])
        if lucky and kind_pre == "lottery":     # "免费使用 + 顺手抽奖"不该按抽奖降权
            ease -= 1.5
        ease = max(-4.0, ease)
        evidence = (1.0 if len(body) >= 200 else 0.0) + (1.0 if len(body) >= 600 else 0.0) + (1.0 if entry else 0.0)
        if len(body) < 40:
            evidence -= 2.0
        if len(links) > 10:                             # 汇总帖（几十个站点的 aff 列表）
            evidence -= 2.5
        recency = 0.5 if (when(champ) or now) >= now - timedelta(days=1) else 0.0
        total = score + ease + evidence + recency
        kind_pre = ("lottery" if ((amount and lucky) or cd_label == "开奖") else "gift" if amount else "free")
        if kind_pre == "free":
            # 免费类：必须有入口、有发放语境，且"具体可得"（正文≥150 字，或 ≥60 字 + 强发放词）
            rich = len(body) >= 150 or (len(body) >= 60 and strong_free)
            if not (entry and gift_evidence and concrete and rich):
                PASSED[key] = False
                continue
        elif total < MIN_SCORE:
            PASSED[key] = False
            continue
        PASSED[key] = True
        champ_time = when(champ) or now
        left = LIFE_DAYS - (now.date() - champ_time.date()).days
        if left <= 0:
            continue
        if cd_deadline and cd_deadline <= now.timestamp():
            continue                                   # 限时已过 → 到点下架（用户口径）
        item = {
            "key": key,
            "brand": (champ.get("offer") or {}).get("brand") or "",
            "amount": ("抽奖 " + amount if (amount and lucky) else amount) or ("限时免费" if "限时" in expiry else "免费试用" if "试用" in expiry else "免费福利"),
            "kind": ("lottery" if ((amount and lucky) or cd_label == "开奖") else "gift" if amount else "free"),
            "unit": unit,
            "score": round(total, 1),
            "level": ("需充值" if hard else "需运气" if lucky else
                      "零门槛" if (free and not [t for t in tags if t in ("需回复", "需邀请码", "需签到", "需拉人", "需手机号", "需绑TG", "需外网")])
                      else "低门槛"),
            "level_class": ("hard" if hard else "luck" if lucky else
                            "free" if (free and not [t for t in tags if t in ("需回复", "需邀请码", "需签到", "需拉人", "需手机号", "需绑TG", "需外网")])
                            else "mid"),
            "reqs": " · ".join(tags[:4]),
            "expiry": expiry,
            "cd_label": cd_label,
            "deadline": cd_deadline,
            "lines": lines,
            "summary_src": src,
            "entry": entry,
            "url": champ.get("url", ""),
            "title": champ.get("title", ""),
            "source": champ.get("source", ""),
            "tags": list(champ.get("tags") or []),
            "when": champ_time.strftime("%m-%d %H:%M"),
            "t": champ_time.timestamp(),
            "n": len(members),
            "on": champ_time.strftime("%m-%d"),
            "left": left,
            "listed_iso": now.date().isoformat(),   # 上架日 = 进板当天（用户口径：上架 15 天）
            "listed_sim": champ_time.date().isoformat(),   # 草稿里按"帖子当天就上架"模拟，正式形态用 listed_iso
            "dups": len(members),
            "dupwhy": "offer",
            "dupkey": "F:" + key,
            "dupsList": [
                {"t": m.get("title", ""), "u": m.get("url", ""), "at": m.get("created_at") or m.get("fetched_at") or ""}
                for m in members
            ],
        }
        # 整卡快照（页面直接用这份渲染；状态文件里也存它 —— 这样 store 把帖子挤出去也不影响显示）
        item["f"] = {
            "name": key, "brand": item["brand"], "amount": item["amount"], "level": item["level"],
            "level_class": item["level_class"], "reqs": item["reqs"], "expiry": item["expiry"],
            "lines": item["lines"], "entry": item["entry"], "when": item["when"], "on": item["on"],
            "cd_label": item["cd_label"], "deadline": item["deadline"], "kind": item["kind"],
            "amt_pre": "抽奖" if str(item["amount"]).startswith("抽奖 ") else "",
            "amt_main": str(item["amount"])[3:] if str(item["amount"]).startswith("抽奖 ") else item["amount"],
            "left": item["left"], "n": item["n"], "src": item["summary_src"],
            # 证据快照：帖子出了 3 天窗口后，靠它复核（否则只能干等到 15 天）
            "ev": {"body": len(body), "concrete": concrete, "strong_free": strong_free, "entry": entry},
        }
        items.append(item)
    items.sort(key=lambda x: -x["t"])
    return items[:MAX_ITEMS] if len(items) > MAX_ITEMS else items


# ---------------------------------------------------------------- 页面接入
CHIP_ANCHOR = '<button class="chip" type="button" data-sort="score">匹配度</button>'
PAYLOAD_ANCHOR = "const state={sort:'latest',"
ENTRIES_ANCHOR = "function makeEntries(){const list=foldCards(cards.filter(matches));"
LABEL_ANCHOR = "function sectionLabel(k){if(k==='recent')return '近 24 小时';"
REFRESH_ANCHOR = "$('#results').replaceChildren();"
COUNT_ANCHOR = "$('#count').textContent=`${state.entries.length} / ${view.length} 条`;"
CARD_ANCHOR = "function renderCard(c){const href=safeURL(c.url),"
FOOTER_ANCHOR = '<footer class="footer">'
DETAILS_ANCHOR = '<details class="hint"><summary>时间与匹配度说明</summary><p>匹配度仅代表标题关键词命中，不代表推荐；日期为北京时间。无发布时间的帖子显示收录时间。同一来源下标题相同的重复帖、以及正文判定为同一份福利的多条帖，都只显示最新一条，角标 ×N 表示折叠条数。</p></details>'
DETAILS_HTML = '<details class="hint"><summary>说明 · 时间 · 匹配度 · 折叠 · 精选</summary><p>· 时间：均为北京时间；帖子没写发布时间的，显示"收录"时间。</p><p>· 匹配度：标题命中关键词的个数——只说明标题提过这些词，<b>不代表推荐</b>。</p><p>· 折叠 ×N：同来源同标题、或正文判定为同一份福利的多条帖，只留最新一条；点 <b>▸ ×N</b> 看清单。</p><p>· 精选项目：每天刷新一次，从<b>最近 3 天</b>里挑值得薅的，按最新时间排；额度 / 条件 / 入口 / 有效期都取自帖子<b>正文</b>，不改写、不编数字。</p><p>· 精选上下架：限时活动到点自动下架，其余<b>上架 15 天</b>到期下架；<b>今日上新</b>的会额外置顶一份，原板块里仍然保留。</p></details>'

PICK_CSS = """
/* —— 精选项目（草稿·强调版）—— 只在排序=精选时出现；颜色走主题变量，明暗两套都跟 —— */

/* 排序里的「精选项目」：未选中就是渐变底 + 星标，选中变实心渐变 + 光晕 */
#sorts .chip[data-sort=pick]{border-color:transparent;background:linear-gradient(135deg,#eef0ff,#f6efff);
  color:#4e52c9;font-weight:800;box-shadow:0 2px 10px rgba(78,82,201,.16)}
#sorts .chip[data-sort=pick]::before{content:"✦";margin-right:5px;font-size:.9em}
#sorts .chip[data-sort=pick].active{background:linear-gradient(135deg,#4e52c9,#8b5cf6);color:#fff;
  box-shadow:0 6px 20px rgba(78,82,201,.42)}
html[data-theme=dark] #sorts .chip[data-sort=pick]{background:linear-gradient(135deg,#26264a,#332a55);color:#c9c6ff}
html[data-theme=dark] #sorts .chip[data-sort=pick].active{background:linear-gradient(135deg,#5b5ff0,#9d7bff);color:#fff}

/* 说明条 */
.pick-note{margin:0 0 12px;padding:10px 12px;border:1px dashed var(--line);border-radius:12px;
  background:var(--soft);color:var(--muted);font-size:.78rem;line-height:1.7}
.pick-note b{color:var(--text)}
.pick-note .k{color:var(--primary);font-weight:700}

/* 卡片：左侧强调色条 + 极浅渐变底 + 悬停抬升发光 */
.pick-card{position:relative;overflow:hidden;gap:9px;padding:16px 16px 14px;
  background:linear-gradient(158deg,var(--surface) 34%,var(--soft) 145%);border-color:#e3e7fb;
  box-shadow:0 6px 22px rgba(78,82,201,.10);transition:border-color .16s,box-shadow .16s,transform .16s}
.pick-card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;
  background:linear-gradient(180deg,#4e52c9,#8b5cf6)}
.pick-card.luck:before{background:linear-gradient(180deg,#f59e0b,#fb7185)}
.pick-card:hover{transform:translateY(-2px);border-color:#c7cdf7;box-shadow:0 14px 34px rgba(78,82,201,.20)}

/* 头行：名字 + 今日上新 …… 额度（主角：实色胶囊，见下方 .pick-amt） */
.pick-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;min-width:0}
.pick-id{display:flex;align-items:center;gap:6px;min-width:0;flex-wrap:wrap}
.pick-name{font-size:1.04rem;font-weight:850;letter-spacing:-.01em;min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;max-width:100%}
.pick-new{font-size:.66rem;font-weight:800;color:#fff;background:linear-gradient(135deg,#e0479e,#f472b6);
  padding:2px 8px;border-radius:999px;white-space:nowrap;
  border:1px solid #ffd6ea;                       /* 浅色描边：在白色卡片上也有对比 */
  box-shadow:0 0 0 2px rgba(255,214,234,.50),0 2px 8px rgba(224,71,158,.30)}
html[data-theme=dark] .pick-new{border-color:#ffd6ea;box-shadow:0 0 0 2px rgba(255,214,234,.20),0 2px 8px rgba(0,0,0,.35)}
html[data-theme=dark] .pick-updated{border:1px solid #bfe6f7;box-shadow:0 0 0 2px rgba(191,230,247,.20),0 2px 8px rgba(14,165,233,.25)}
/* 金额：实色胶囊（浅底 + 深紫字 + 描边）。弃用 background-clip:text 渐变裁字 ——
   那个技法在不同渲染器下会翻车（用户两次反馈"渲染有问题"），实色永远一致且更重。 */
.pick-amt{display:inline-flex;align-items:baseline;gap:2px;padding:2px 9px;border-radius:10px;
  font-size:1.42rem;line-height:1.15;font-weight:900;letter-spacing:-.02em;white-space:nowrap;
  color:#4c1d95;background:#f0ecff;border:1px solid #ddd4ff;box-shadow:0 1px 0 rgba(76,29,149,.06) inset}
.pick-amt .pre{font-size:.72rem;font-weight:800;letter-spacing:0;margin-right:3px;color:#b45309}
.pick-amt .pre{font-size:.74rem;font-weight:800;letter-spacing:0;margin-right:3px;
  background:none;-webkit-text-fill-color:currentColor;color:#b45309}
html[data-theme=dark] .pick-amt{color:#d8d2ff;background:#2b2550;border-color:#4a3f8f}

/* 门槛/品牌/有效期/折叠：实心或描边小胶囊 */
.pick-meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pick-tag{font-size:.7rem;line-height:1.3;padding:4px 10px;border-radius:999px;font-weight:800;
  border:1px solid transparent;white-space:nowrap}
.pick-tag.free{background:linear-gradient(135deg,#0ea56b,#22c55e);color:#fff;box-shadow:0 2px 10px rgba(16,185,129,.30)}
.pick-tag.mid{background:#fff4dd;color:#a15c00;border-color:#f2d9a4}
.pick-tag.luck{background:linear-gradient(135deg,#f59e0b,#fb7185);color:#fff;box-shadow:0 2px 10px rgba(245,158,11,.32)}
.pick-tag.hard{background:#fdeaea;color:#a02222;border-color:#f2c9c9}
.pick-sub{font-size:.7rem;color:var(--hint);border:1px solid var(--line);border-radius:999px;
  padding:3px 8px;white-space:nowrap;background:var(--surface)}
html[data-theme=dark] .pick-tag.free{background:linear-gradient(135deg,#0f8f5e,#1fae57)}
html[data-theme=dark] .pick-tag.mid{background:#3a2f16;color:#f0cf90;border-color:#6b5730}
html[data-theme=dark] .pick-tag.luck{background:linear-gradient(135deg,#b9770b,#c2555f)}
html[data-theme=dark] .pick-tag.hard{background:#3a1f1f;color:#f0b0b0;border-color:#6e3a3a}

/* 正文：第一句="讲啥"（加粗），数字自动高亮 */
.pick-lead{margin:2px 0 0;font-size:.92rem;line-height:1.5;font-weight:700;color:var(--text)}
.pick-sub2{margin:0;font-size:.83rem;line-height:1.5;color:var(--muted)}
.pick-card .hl{font-style:normal;font-weight:850;color:var(--primary);
  background:linear-gradient(transparent 62%,rgba(78,82,201,.18) 62%);padding:0 1px;border-radius:2px}
html[data-theme=dark] .pick-card .hl{color:#b9beff;background:linear-gradient(transparent 62%,rgba(139,146,255,.24) 62%)}
.pick-req{margin-top:2px;font-size:.74rem;color:var(--hint)}
.pick-req b{color:var(--muted);font-weight:700}

/* 脚行：入口 / 原帖 / 剩余天数 */
/* 分割线对齐的关键：**空白插在脚行上方**（.pick-foot 吃 margin-top:auto），
   脚行+按钮作为一个整体贴住卡片底部 → 同一行两张卡的虚线、入口网址、按钮都在同一高度。
   （反过来把 auto 给按钮的话，脚行仍跟在内容后面，高度就会随内容长度飘。） */
.pick-foot{margin-top:auto;padding-top:10px;border-top:1px dashed var(--line);display:flex;flex-wrap:wrap;
  gap:6px 12px;align-items:center;font-size:.72rem;color:var(--hint)}
.pick-entry{font-weight:800;color:var(--primary);letter-spacing:.01em}
.pick-src{color:var(--muted);text-decoration:none;border-bottom:1px dotted var(--line)}
.pick-src:hover{color:var(--primary)}
.pick-life{margin-left:auto;white-space:nowrap;font-variant-numeric:tabular-nums;font-weight:700;
  color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:2px 9px;background:var(--surface)}
.pick-life.soon{color:#b45309;border-color:#f2d9a4;background:#fff4dd}
html[data-theme=dark] .pick-life.soon{color:#f0b463;border-color:#6b5730;background:#3a2f16}

/* 倒计时胶囊：正常=琥珀，≤1 小时=红，已结束=灰划掉；整卡过期时额度降透明度 */
.pick-cd{font-size:.7rem;line-height:1.3;padding:4px 10px;border-radius:999px;font-weight:800;white-space:nowrap;
  font-variant-numeric:tabular-nums;color:#b45309;background:#fff4dd;border:1px solid #f2d9a4}
.pick-cd::before{content:"⏳";margin-right:4px;font-size:.92em}
.pick-cd.urgent{color:#fff;background:linear-gradient(135deg,#ef4444,#f97316);border-color:transparent;
  box-shadow:0 2px 10px rgba(239,68,68,.35)}
.pick-cd.done{color:var(--hint);background:var(--hover);border-color:var(--line);text-decoration:line-through}
.pick-cd.done::before{content:"⛔"}
.pick-card.done .pick-amt{opacity:.5;filter:saturate(.6)}
.pick-card.done .pick-lead{color:var(--muted)}
html[data-theme=dark] .pick-cd{color:#f0cf90;background:#3a2f16;border-color:#6b5730}
html[data-theme=dark] .pick-cd.urgent{color:#fff;background:linear-gradient(135deg,#c0392b,#c2555f);border-color:transparent}
html[data-theme=dark] .pick-cd.done{color:var(--hint);background:#22242e;border-color:#3a3d4a}

@media(max-width:760px){
  .pick-card{padding:14px 14px 12px}
  .pick-name{font-size:.98rem}
  .pick-amt{font-size:1.5rem}
  .pick-lead{font-size:.88rem}
  .pick-note{font-size:.75rem}
}

/* 长条「立即跳转」按钮：整行、渐变、悬停抬升 + 箭头滑出、按下回弹 */
.pick-go{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;margin-top:10px;
  padding:11px 14px;border-radius:12px;font-weight:850;font-size:.9rem;letter-spacing:.02em;
  color:#fff;text-decoration:none;background:linear-gradient(135deg,#4e52c9,#8b5cf6);
  box-shadow:0 6px 18px rgba(78,82,201,.32);
  transition:transform .14s,box-shadow .14s,filter .14s}
.pick-go .arw{transition:transform .16s}
.pick-go:hover{transform:translateY(-1px);box-shadow:0 10px 26px rgba(78,82,201,.42);filter:brightness(1.06)}
.pick-go:hover .arw{transform:translateX(4px)}
.pick-go:active{transform:translateY(0) scale(.995);box-shadow:0 4px 12px rgba(78,82,201,.30)}
html[data-theme=dark] .pick-go{background:linear-gradient(135deg,#5b5ff0,#9d7bff);box-shadow:0 6px 18px rgba(120,120,255,.30)}
.pick-updated{font-size:.68rem;font-weight:800;color:#fff;background:linear-gradient(135deg,#0ea5e9,#38bdf8);
  padding:3px 8px;border-radius:999px;white-space:nowrap;box-shadow:0 2px 8px rgba(14,165,233,.30)}
/* 卡片脚行改为"入口 + 剩余天数"一行，之后接长条按钮 */
.pick-foot{margin-top:auto;padding-top:10px;border-top:1px dashed var(--line)}

/* 三个筛选行（排序/来源/类型）统一做小一档 —— 这是页面级微调，正式上线时搬进 template.html */
.chip{min-height:32px;padding:0 11px;font-size:.78rem;border-radius:9px}
.filter-row{min-height:38px}
.filter-name{font-size:.75rem}
@media(max-width:760px){.chip{min-height:38px}.filter-row{min-height:42px}}

/* ================= 布局方案样式 ================= */
.pk-toggle{margin-top:9px;width:100%;min-height:38px;border:1px dashed var(--line);border-radius:11px;background:var(--surface);
  color:var(--primary);font-size:.78rem;font-weight:750}
.pk-toggle:hover{border-color:var(--primary);background:var(--soft)}
.pk-nav{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}
.pk-nav-btn{display:inline-flex;align-items:center;gap:6px;font-size:.74rem;font-weight:750;padding:6px 11px;border-radius:999px;
  border:1px solid var(--line);background:var(--surface);color:var(--muted)}
.pk-nav-btn:hover{border-color:var(--primary);color:var(--primary)}
.pk-nav-btn .n{font-size:.68rem;font-weight:800;color:var(--primary);background:var(--soft);border-radius:999px;padding:1px 6px}
/* 精选视图：板块上下距离收紧（默认 17px → 13px） */
#results.pick-mode .group{margin-top:13px}
#results.pick-mode .group-head{margin-bottom:8px}
/* 货架：每区一行、横向滑动（高度 = 一张卡） */
#results.pick-rail .group .cards{display:flex;gap:10px;overflow-x:auto;overflow-y:hidden;padding-bottom:6px;
  scroll-snap-type:x proximity;scrollbar-width:thin}
#results.pick-rail .group .cards>.pick-card{flex:0 0 min(520px,86%);scroll-snap-align:start}
#results.pick-rail .group .cards::-webkit-scrollbar{height:8px}
#results.pick-rail .group .cards::-webkit-scrollbar-thumb{background:var(--line);border-radius:99px}
@media(max-width:760px){#results.pick-rail .group .cards>.pick-card{flex:0 0 88%}}

/* 手机端：排序行右侧的"199 / 199 条"会挤住（甚至盖住）排序按钮 —— 隐藏。
   信息列表上方已有"显示 N / N 条"（#result-summary），这里不丢信息。
   （页面级微调；正式上线时搬进 template.html 的移动端媒体查询） */
@media(max-width:760px){#count{display:none}}
"""

PICK_JS = r"""
/* —— 精选项目（草稿·强调版 v3）—— 三个板块（免费/赠送/抽奖）+ 到期自动下架 —— */
const PICK_NOTE = '<div class="pick-note"><b>✦ 精选项目</b> · 每日刷新一次：从<span class="k">最近 3 天</span>的帖子里筛出值得薅的鸡蛋，'
 + '按<span class="k">最新时间</span>排序，<span class="k">限时活动到点自动下架</span>、其余<span class="k">上架 15 天</span>到期下架。'
 + '额度/条件/入口/有效期都从帖子<span class="k">正文</span>里抽取（不改写、不编数字）；「▸ ×N」是同一个活动的其它帖子。'
 + '<br><b>今日上新</b>的会复制一份<span class="k">置顶</span>，原板块里仍然保留。</div>';

/* 数字高亮：先按"金额/额度"切段，再逐段转义 —— 不要先转义再匹配，否则 &quot; 里的 39 会被当成数字 */
const HL_RE = /((?:[$￥¥])\s?\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:亿|万|刀|美刀|美元|元|token|词元))/g;
function hlText(s){return String(s||'').split(HL_RE)
  .map((part,i)=> i%2 ? '<span class="hl">'+escapeHTML(part)+'</span>' : escapeHTML(part)).join('')}

/* 分板块：免费 → 赠送 → 抽奖（板块内仍是"最新在前"）。分类在生成器里定好（f.kind），
   这里只负责按固定顺序拼；到点下架过的记在 PICK_EXPIRED，重渲染也不会再冒出来。 */
const PICK_KINDS = ['gift','lottery','free'];   /* 用户口径（第三版）：赠送 → 抽奖 → 免费 */
const PICK_EXPIRED = new Set();
function pickEntries(){
  const hit=FEATURED.filter(c=>matches(c) && !PICK_EXPIRED.has(c.dupkey));
  /* 今日上新的卡**复制一份**置顶（原板块里保留）：同一个 dupkey 会出现两次，
     所以下面的计数一律按去重后的 key 算，到期下架也要把所有副本一起摘掉。 */
  const pinned=hit.filter(c=>c.f.left>=15).map(c=>({c,key:'pick:new'}));
  const rest=PICK_KINDS.flatMap(k=>hit.filter(c=>c.f.kind===k).map(c=>({c,key:'pick:'+k})));
  return pinned.concat(rest);
}
/* 去重计数：置顶副本不重复算一项 */
function pickCount(){return new Set([...document.querySelectorAll('.pick-card')].map(c=>c.dataset.pickKey)).size}

/* 倒计时：截止时刻是**绝对时间戳**（生成时按"发帖时间 + 帖子里写的限时"算好），
   所以页面重新生成之前它也不会跑偏；前端每秒轮询刷新（用 setInterval，不用 rAF —— 见项目坑表 16/17） */
/* 倒计时基准不用访客的绝对时钟（手机时间不准就整条全错），用"烘焙时刻 + 页面已开时长"：
   烘焙时刻由生成器写进 PICK_BAKED_AT（秒），页面重新生成时会自动刷新。 */
const PICK_LOADED_AT=Date.now();
function pickNow(){return PICK_BAKED_AT*1000+(Date.now()-PICK_LOADED_AT)}
function cdText(ms){
  if(ms<=0) return '已结束';
  const s=Math.floor(ms/1000), d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60), sec=s%60;
  if(d>0) return d+' 天'+(h?' '+h+' 小时':'');
  if(h>0) return h+' 小时 '+m+' 分';
  if(m>0) return m+' 分 '+sec+' 秒';
  return sec+' 秒';
}
function cdChip(f){
  if(!f.deadline) return '';
  const ms=f.deadline*1000-pickNow();
  const at=new Date(f.deadline*1000+8*3600e3).toISOString().slice(5,16).replace('T',' ');
  return '<span class="pick-cd'+(ms<3600e3?' urgent':'')+'" data-cd="'+f.deadline+'" data-label="'+escapeHTML(f.cd_label||'截止')+'"'
    +' title="'+escapeHTML((f.cd_label||'截止'))+' '+at+'（北京时间，按发帖时间+帖内限时折算）">'
    + escapeHTML((f.cd_label||'截止'))+' '+cdText(ms)+'</span>';
}
/* 到点即下架：卡片从 DOM 里摘掉，并把该板块的"显示 x / y"与顶部计数一起修正 */
function dropExpiredCard(card, key){
  if(key) PICK_EXPIRED.add(key);
  /* 置顶副本 + 原板块副本：同一个 key 的全部元素一起摘掉 */
  const cards=key?[...document.querySelectorAll('.pick-card[data-pick-key="'+key+'"]')]:(card?[card]:[]);
  const touchedSections=new Set();
  for(const el of cards){const sec=el.closest('.group'); if(sec) touchedSections.add(sec); el.remove();}
  for(const sec of touchedSections){
    const left=sec.querySelectorAll('.pick-card').length;
    const portion=sec.querySelector('.portion');
    const m=portion&&portion.textContent.match(/\/\s*(\d+)/);
    if(portion) portion.textContent='显示 '+left+' / '+(m?m[1]:left)+' 条';
    if(!left) sec.remove();
  }
  const c=document.querySelector('#count');
  if(c) c.textContent='精选 '+pickCount()+' 项';
}
function tickPickCountdowns(){
  const now=pickNow();
  document.querySelectorAll('.pick-cd[data-cd]').forEach(el=>{
    const ms=Number(el.dataset.cd)*1000-now;
    const card=el.closest('.pick-card');
    if(ms<=0){ dropExpiredCard(card, card&&card.dataset.pickKey); return; }
    el.textContent=(el.dataset.label||'截止')+' '+cdText(ms);
    el.classList.toggle('urgent', ms<3600e3);
  });
}
setInterval(()=>{if(!document.hidden) tickPickCountdowns()},1000);

function pickCardHTML(c){const f=c.f;const href=safeURL(c.url);
 const lifeLeft=f.left<=3?' soon':'';
 const fresh=f.left>=15?'<span class="pick-new">今日上新</span>':'';
 const amt=f.amt_pre?('<span class="pre">'+escapeHTML(f.amt_pre)+'</span>'+escapeHTML(f.amt_main))
                    :escapeHTML(f.amount||'免费福利');
 const lead=f.lines[0]?hlText(f.lines[0]):'';
 const sub=f.lines[1]?hlText(f.lines[1]):'';
 return '<article class="card pick-card'+(f.level_class==='luck'?' luck':'')+'" data-pick-key="'+escapeHTML(String(c.dupkey))+'" data-pick-kind="'+escapeHTML(f.kind)+'">'
  + '<div class="pick-head"><span class="pick-id"><span class="pick-name">'+escapeHTML(f.name)+'</span>'+fresh+'</span>'
  + '<span class="pick-amt">'+amt+'</span></div>'
  + '<div class="pick-meta"><span class="pick-tag '+escapeHTML(f.level_class)+'">'+escapeHTML(f.level)+'</span>'
  + cdChip(f)
  + (f.brand?'<span class="pick-sub">'+escapeHTML(f.brand)+'</span>':'')
  + (f.expiry?'<span class="pick-sub">'+escapeHTML(f.expiry)+'</span>':'')
  + (f.updated?'<span class="pick-updated" title="同一个活动后来又发了新帖，卡片已按最新那条重新总结">已更新</span>':'')
  + (f.n>1?'<button class="dups hot" type="button" data-fold="'+escapeHTML(String(c.dupkey))+'" data-n="'+f.n+'"'
      +' aria-haspopup="dialog" title="同一个活动共 '+f.n+' 条，点击展开清单">▸ ×'+f.n+'</button>':'')
  + '</div>'
  + (lead?'<p class="pick-lead">'+lead+'</p>':'')
  + (sub?'<p class="pick-sub2">'+sub+'</p>':'')
  + '<div class="pick-req">条件：<b>'+escapeHTML(f.reqs||'见原帖')+'</b></div>'
  + '<div class="pick-foot">'
  + (f.entry?'<span class="pick-entry">'+escapeHTML(f.entry)+'</span>':'')
  + '<span class="pick-life'+lifeLeft+'" title="上架 '+escapeHTML(f.on)+' · 上架 15 天到期下架">'+(f.left>0?('剩 '+f.left+' 天'):'即将下架')+'</span>'
  + '</div>'
  + (href?'<a class="pick-go" href="'+escapeHTML(href)+'" target="_blank" rel="noopener noreferrer"'
       +' title="打开原帖（新标签页）· 摘录自 '+escapeHTML(f.when)+'"><span>立即跳转</span><span class="arw" aria-hidden="true">→</span></a>':'')
  + '</article>'}


/* ================= 布局方案（草稿里可切换；选定后把默认值写死、并移除这排按钮） =================
   问题：板块一多，下面的板块被顶到几屏之外，看不到"还有别的板块"。四个方案：
     cap  每区先给 4 张 + "展开本板块全部 N 条"（最容易一眼看全所有板块）
     rail 每区一行货架、横向滑动（高度=一张卡，所有板块标题都在首屏）
     jump 平铺 + 顶部"板块导航"锚点（点一下跳到某区，内容不动）
     all  全部平铺（现状）
   选择存 localStorage，刷新后保持。 */
let PICK_LAYOUT = 'jump';   /* 用户 2026-10-16 选定的方案：平铺 + 顶部板块导航 */
const PICK_CAP = 4;                       /* cap 方案每区首屏显示几张（4 = 两行） */

function pickNavHtml(){
  return PICK_LAYOUT === 'jump' ? '<div class="pk-nav" id="pk-nav"></div>' : '';
}

function pickApplyLayout(){
  const res = document.querySelector('#results');
  if (!res) return;
  res.classList.toggle('pick-mode', state.sort === 'pick');
  if (state.sort !== 'pick') {
    res.classList.remove('pick-rail');
    const nav = document.querySelector('#pk-nav'); if (nav) nav.remove();
    return;
  }
  res.classList.toggle('pick-rail', PICK_LAYOUT === 'rail');
  res.querySelectorAll('.pk-toggle').forEach(b => b.remove());
  res.querySelectorAll('.pick-card[data-pk-hidden]').forEach(c => { c.removeAttribute('data-pk-hidden'); c.style.display = ''; });
  const nav = document.querySelector('#pk-nav');
  if (PICK_LAYOUT === 'jump') {
    if (nav) {
      const groups = [...res.querySelectorAll('.group')].filter(g => g.dataset.key !== 'pick:new');
      nav.innerHTML = groups.map(g => {
        const h = g.querySelector('h2').textContent;
        return '<button type="button" class="pk-nav-btn" data-pk-jump="' + escapeHTML(h) + '">' + escapeHTML(h)
          + '<span class="n">' + g.querySelectorAll('.pick-card').length + '</span></button>';
      }).join('');
    }
  } else if (nav) nav.remove();
  if (PICK_LAYOUT !== 'cap') return;
  res.querySelectorAll('.group').forEach(sec => {
    const cards = [...sec.querySelectorAll('.pick-card')];
    if (cards.length <= PICK_CAP) return;
    cards.slice(PICK_CAP).forEach(c => { c.setAttribute('data-pk-hidden', '1'); c.style.display = 'none'; });
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'pk-toggle'; btn.dataset.pkToggle = '1';
    btn.dataset.total = String(cards.length); btn.dataset.cap = String(PICK_CAP); btn.dataset.open = '0';
    btn.textContent = '展开本板块全部 ' + cards.length + ' 条 ▾';
    (sec.querySelector('.cards') || sec).insertAdjacentElement('afterend', btn);
  });
}

/* 跳转必须"真的到位"：页面有 html{scroll-behavior:smooth}，连 scrollTo(0,X) 的瞬移形式
   也会被它变成平滑滚动 —— 而后台/被限流的标签页里平滑滚动不推进（实测 scrollY 一动不动）。
   所以：可见且未要求减动效时走平滑；否则（或 350ms 没动）临时把 CSS 行为改成 auto 强制瞬移。 */
function pkJumpTo(sec){
  const top = Math.round(sec.getBoundingClientRect().top + scrollY - 70);
  const before = window.scrollY;
  const root = document.documentElement;
  const force = () => {
    const prev = root.style.scrollBehavior;
    root.style.scrollBehavior = 'auto';
    window.scrollTo(0, top);
    if (prev) root.style.scrollBehavior = prev; else root.style.removeProperty('scroll-behavior');
  };
  if (document.hidden || matchMedia('(prefers-reduced-motion:reduce)').matches) { force(); return; }
  window.scrollTo({ top, behavior: 'smooth' });
  setTimeout(() => { if (Math.abs(window.scrollY - before) < 4) force(); }, 350);
}

document.addEventListener('click', e => {
  const b = e.target.closest && e.target.closest('.pk-toggle,.pk-nav-btn');
  if (!b) return;
  if (b.classList.contains('pk-nav-btn')) {
    const sec = [...document.querySelectorAll('#results .group')].find(g => g.querySelector('h2').textContent === b.dataset.pkJump);
    if (sec) pkJumpTo(sec);
    return;
  }
  const sec = b.closest('.group'); if (!sec) return;
  const open = b.dataset.open === '1';
  b.dataset.open = open ? '0' : '1';
  [...sec.querySelectorAll('.pick-card')].slice(Number(b.dataset.cap)).forEach(c => {
    if (open) { c.setAttribute('data-pk-hidden', '1'); c.style.display = 'none'; }
    else { c.removeAttribute('data-pk-hidden'); c.style.display = ''; }
  });
  b.textContent = open ? ('展开本板块全部 ' + b.dataset.total + ' 条 ▾') : '收起 ▴';
});

/* 页面自己重渲染（切排序/筛选/到点下架）之后，把布局再套一遍 */
let pkMo = null;
function pkWatch(){
  if (pkMo) return;
  const res = document.querySelector('#results'); if (!res) return;
  let timer = null;
  pkMo = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(pickApplyLayout, 40); });
  pkMo.observe(res, { childList: true, subtree: false });
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => { pkWatch(); setTimeout(pickApplyLayout, 300); });
else { pkWatch(); setTimeout(pickApplyLayout, 300); }
"""


def patch(html_text: str, payload: list[dict], baked_at: float,
          layout: str = "", open_pick: bool = False) -> str:
    def once(src: str, old: str, new: str) -> str:
        assert src.count(old) == 1, f"锚点不唯一/缺失：{old[:60]}（{src.count(old)}）"
        return src.replace(old, new, 1)

    # 烘焙时刻：倒计时的基准（不信任访客的绝对时钟，见 pick_script.js 的 pickNow()）
    payload_js = ("const PICK_BAKED_AT=" + str(int(baked_at)) + ";\n"
                  + "const FEATURED=" + json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c") + ";\n")
    out = html_text
    out = once(out, CHIP_ANCHOR, CHIP_ANCHOR + '<button class="chip" type="button" data-sort="pick">精选项目</button>')
    out = once(out, PAYLOAD_ANCHOR, payload_js + PICK_JS + PAYLOAD_ANCHOR)
    out = once(out, "</style>", PICK_CSS + "</style>")
    # 在函数体开头插分支：替换串只能保留锚点里"{ 之后"的部分，别把函数头抄第二遍
    # （第一版就是这么错的：`function makeEntries(){…` 后跟第二个 `function makeEntries(){…`，
    #  外层函数永远缺收尾花括号 → 整段脚本 SyntaxError、页面所有 JS 都不执行，
    #  而所有锚点断言照样通过——断言只证明"锚点在"，不证明"替换构造成立"）
    entries_body = ENTRIES_ANCHOR.replace("function makeEntries(){", "", 1)
    out = once(out, ENTRIES_ANCHOR,
               "function makeEntries(){if(state.sort==='pick'){return pickEntries()}\n"
               + entries_body)
    out = once(out, LABEL_ANCHOR, LABEL_ANCHOR + "if(k==='pick:new')return '今日上新 · 置顶';"
               + "if(k==='pick:free')return '免费 · 注册或直接用';"
               + "if(k==='pick:gift')return '赠送 · 送到账';if(k==='pick:lottery')return '抽奖 · 拼手气';")
    out = once(out, REFRESH_ANCHOR, REFRESH_ANCHOR + "if(state.sort==='pick')$('#results').insertAdjacentHTML('afterbegin',pickNavHtml());")
    out = once(out, COUNT_ANCHOR, COUNT_ANCHOR.replace("`${state.entries.length} / ${view.length} 条`",
                                                       "state.sort==='pick'?`精选 ${pickCount()} 项`:`${state.entries.length} / ${view.length} 条`"))
    out = once(out, CARD_ANCHOR, "function renderCard(c){if(c.f)return pickCardHTML(c);const href=safeURL(c.url),")
    if layout:      # 固定方案 + 不读 localStorage（否则先开过的文件会把它的选择带过来）
        old_default = "let PICK_LAYOUT = 'jump';   /* 用户 2026-10-16 选定的方案：平铺 + 顶部板块导航 */"
        out = once(out, old_default,
                   "let PICK_LAYOUT = " + repr(layout) + ";   /* 本文件固定为 " + layout + " 方案（离线预览） */")
        label = {"cap": "每区 4 张+展开", "rail": "货架横滑", "jump": "锚点跳转", "all": "全部平铺"}[layout]
        out = once(out, "</title>", "（精选布局：" + label + "）</title>")
    if open_pick:   # 预览文件打开就进精选
        out = once(out, "init();",
                   "init();\nstate.sort='pick'; try{ selected('sorts','sort','pick'); refresh(); }catch(e){}")
    out = once(out, DETAILS_ANCHOR, DETAILS_HTML)   # 折叠说明：把精选口径并进去、重写话术
    out = once(out, FOOTER_ANCHOR,
               FOOTER_ANCHOR + '\n<p id="draft-note">本页是<b>离线草稿</b>：排序里新增了「精选项目」，数据为最近 3 天实拍（未上线）。</p>')
    return out


def merge_board(state: dict, fresh: list[dict], now: datetime,
                 recheck: dict | None = None) -> tuple[list[dict], dict]:
    """精选板 = 旧板（≤15 天、未过期）+ 本次新入选；同活动有更新的帖 → 换原帖重总结，但**不重算上架日**。

    为什么要有这层：主信息列表只留 300 行（约 60 天滚动窗口），而精选板承诺"上架 15 天"。
    一旦某条帖被 store 挤出去（或被过滤），只要它还在板上，就必须照常显示 —— 所以卡片数据
    存在板上（featured-state.json），不依赖 topics.jsonl。
    """
    fresh_by_key = {it["key"]: it for it in fresh}
    recheck = recheck or {}
    board, stats = [], {"kept": 0, "updated": 0, "added": 0, "dropped": 0, "pruned": 0}
    for key, old in state.items():
        listed = old.get("listed_iso") or old.get("listed_sim") or now.date().isoformat()
        days = (now.date() - datetime.fromisoformat(listed).date()).days
        if days >= LIFE_DAYS:
            stats["dropped"] += 1
            continue
        dl = (old.get("f") or {}).get("deadline")
        if dl and dl <= now.timestamp():
            stats["dropped"] += 1
            continue
        new = fresh_by_key.pop(key, None)
        if new is None and key in recheck and not recheck[key]:
            stats["dropped"] += 1
            stats["pruned"] += 1
            continue                                           # 帖子还在库里但已过不了新门槛 → 下架
        if new is None and key not in recheck:                 # 帖子出了 3 天窗口 → 凭快照复核
            f_old = old.get("f") or {}
            ev = f_old.get("ev") or {}
            kind_old = f_old.get("kind")
            if kind_old in ("gift", "lottery") and (old.get("score") or 0) < MIN_SCORE:
                stats["dropped"] += 1
                stats["pruned"] += 1
                continue
            if kind_old == "free":
                if ev:
                    rich_old = ev.get("body", 0) >= 150 or (ev.get("body", 0) >= 60 and ev.get("strong_free"))
                    if not (ev.get("entry") and ev.get("concrete") and rich_old):
                        stats["dropped"] += 1
                        stats["pruned"] += 1
                        continue
                elif f_old.get("src") == "title":              # 老条目无快照：当年正文不可用的不再保
                    stats["dropped"] += 1
                    stats["pruned"] += 1
                    continue
        if new:
            merged = dict(new)
            merged["listed_iso"] = listed                      # 上架日沿用第一次进板那天
            merged["on"] = datetime.fromisoformat(listed).strftime("%m-%d")
            merged["left"] = LIFE_DAYS - days
            merged["f"] = dict(new["f"])
            merged["f"]["on"] = merged["on"]
            merged["f"]["left"] = merged["left"]
            if new["t"] > old.get("t", 0):
                merged["f"]["updated"] = True                  # 有更新的同活动帖 → 卡片已刷新
                stats["updated"] += 1
            board.append(merged)
        else:
            old["left"] = LIFE_DAYS - days
            old["f"]["left"] = old["left"]
            board.append(old)                                  # store 里已经没有它了，照样留着
            stats["kept"] += 1
    for it in fresh_by_key.values():
        board.append(it)
        stats["added"] += 1
    board.sort(key=lambda x: -x["t"])
    return board, stats
