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
    stats["candidates"] = len(PASSED) + len(REJECT_REASON)
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
MIN_AMOUNT_USD = 10.0     # 币种额度下限（用户口径：限制10$）；token 类不折算、不受此限

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
# token 量纲族：每项带「到亿系数」——内部统一为亿，显示统一为 B(十亿)/M(百万)/K(千)。
# B/M/K 裸写（无 token 后缀）靠发放语境窗口防误伤（"送5M"=5M token，圈子默认写法）；
# "5km""10MB" 这类由字母边界 (?![a-zA-Z]) 排除。
AMOUNT_PATTERNS = (
    (re.compile(r"(?:[$￥¥])\s?(\d+(?:\.\d+)?)"), "usd", None),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[$￥¥]"), "usd", None),          # 2$ / 2￥（数字在前，实测常写）
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:刀|美刀|美元|usd)", re.I), "usd", None),
    (re.compile(r"(\d+(?:\.\d+)?)\s*元"), "cny", None),
    (re.compile(r"(\d+(?:\.\d+)?)\s*亿"), "token", 1.0),                              # N亿 → N 亿
    (re.compile(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])"), "token", 10.0),               # NB → N×10 亿
    (re.compile(r"(\d+(?:\.\d+)?)\s*[mM](?![a-zA-Z])"), "token", 0.01),               # NM → N×0.01 亿（=N百万）
    (re.compile(r"(\d+(?:\.\d+)?)\s*[kK](?![a-zA-Z])"), "token", 0.0001),             # NK → N×0.0001 亿（=N千）
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:万|w|W)\s*(?:token|词元|额度)"), "token", 0.0001),  # N万token
)
LINE_SPLIT = re.compile(r"[\n\r]+|(?<=[。！!？?;；])\s*")
# 上限词：只看数字**前面**的窗口——「最高可🉐300亿」的 300亿 是上限；
# 「30亿token，最高可🉐300亿」里的最高管的是后面的 300亿，不关 30亿 的事。
# 数字后面的「封顶/为止」另算（也封这一个数字）。
CAP_HINT = re.compile(r"最高|至多|可🉐|上限|不超过|最大")
CAP_AFTER = re.compile(r"封顶|为止")
# token 语境词：token 候选（B/M/K/亿/万）的窗口凭它自证是"额度"而非别处的数字
TOKEN_CTX = re.compile(r"token|词元|算力|模型|额度|积分|api|key|接口", re.I)
# 价格/门槛语境：数字附近出现这些词说明它是售价或消费门槛，不是发放额度
PRICE_CTX = re.compile(r"起|折|倍率|门槛|满|消费|付款|付费|购买|套餐|会员|订阅|价格|售价|原价|月费|年费|续费")


def _band_usd(v: float) -> float:
    for cut, score in ((200, 6), (100, 5), (50, 4.5), (20, 4), (10, 3), (5, 2), (2, 1)):
        if v >= cut:
            return float(score)
    return 0.0


def _display_amount(raw: str) -> str:
    """金额显示统一为 数字+单位符号（用户 2026-10-17 口径）：刀/美元/美刀 → $，元 → ￥，挤掉空格。"""
    s = re.sub(r"\s+", "", raw)
    return s.replace("美元", "$").replace("美刀", "$").replace("刀", "$").replace("元", "￥")


def _near(pattern, window: str, anchor: int):
    """窗口内 pattern 各命中点离 anchor 的最短距离；无命中返回 None。"""
    hits = [mm.start() for mm in pattern.finditer(window)]
    return min((abs(h - anchor) for h in hits), default=None)


def _display_token(v_yi: float) -> str:
    """token 量显示统一为 B(十亿)/M(百万)/K(千)：30亿→3B、500万→5M、2000→2K。低于 1000 → 裸数。"""
    if v_yi >= 10:
        return f"{v_yi / 10:g}B"
    if v_yi >= 0.01:
        return f"{v_yi * 100:g}M"
    if v_yi >= 0.0001:
        return f"{v_yi * 10000:g}K"
    return f"{v_yi * 1e8:g}"


def amount_of(text: str) -> tuple[float, str, str, float | None]:
    """(排序分, 展示文本, 单位, 折算美元值)。展示统一为 数字+单位符号（R6：刀/美元→$，元→￥）。
    额度必须出现在"发放语境"±10 字内。

    为什么用窗口而不是"同一行"：真实帖子常把量值和发放词拆到相邻行
    （实测 lunarfox：「666刀」/「纯heavy号池」/「限时12个小时」各占一行），
    按行判会整条漏掉；而广告（gt.tc「低至 ¥0.0211/刀起」）在 ±40 字窗口里
    没有任何发放词，仍然拦得住。
    """
    t = CLEAN_URL.sub(" ", text or "")
    best = (0.0, "", "", None)        # 全体候选（含上限值/拉新值）的最大分
    best_plain = None                 # 非"上限值"、非"拉新值"候选里分数最高的
    best_ref = None                   # 拉新语境候选（「拉新一人可领120刀」——要拉人才拿得到，显示降权）
    for pat, unit, yi in AMOUNT_PATTERNS:
        for m in pat.finditer(t):
            # 发放词必须**紧贴**数字（±10 字）：±40 那种宽窗口会把奖励表里的
            # "消费 $200 固定返 $10" 当成"送 $200"（nexotoken 实测踩过）。
            w_start = max(0, m.start() - 10)
            window = t[w_start:m.end() + 10]
            anchor = m.start() - w_start
            # token 候选（B/M/K/亿/万token）的语境自带自证：数字旁边就有 Tokens/词元 字样
            # （omnirush 实测：「解锁 额外 (5M) Tokens 邀请奖励」±10 字内没有发放词，
            #  按 GIFT_WORD 会被否决，只剩带「最高」的 500M 幸存 → 整卡被带偏）。
            gift_d = _near(GIFT_WORD, window, anchor)
            ok_ctx = bool(GIFT_WORD.search(window)) or (
                unit == "token" and TOKEN_CTX.search(window))
            if not ok_ctx:
                continue
            # 价格否决用「最近语境」判定：窗口里可能同时有发放词和价格词
            # （实测：「免费领取20美元额度，月费99美元」——20美元 的 ±6 窗口
            #  会够到邻居的「月费」，把合法额度误杀）。离数字更近的语境说了算。
            price_d = _near(PRICE_CTX, window, anchor)
            if price_d is not None and (gift_d is None or price_d < gift_d):
                continue                                     # 售价 / 消费门槛，不是发放额度
            v = float(m.group(1))
            usd = None
            if unit == "usd":
                score = _band_usd(v)
                usd = v
            elif unit == "cny":
                score = _band_usd(v / CNY_PER_USD)
                usd = v / CNY_PER_USD
            else:  # token 量纲族：按系数折到亿，显示统一 B/M/K
                v_yi = v * yi
                score = _band_usd(v_yi / 10.0) if v_yi >= 10 else (1.5 if v_yi >= 3 else 1.0)
            display = _display_token(v_yi) if unit == "token" else _display_amount(m.group(0).strip())
            cand = (score, display, unit, usd)
            if score > best[0]:
                best = cand
            capped = bool(CAP_HINT.search(t[max(0, m.start() - 16):m.start()])) or bool(CAP_AFTER.search(t[m.end():m.end() + 6]))
            # 拉新词要看数字**前面**的窗口（「拉新一人可领120刀」）——「注册送10刀，拉新一人再送100刀」
            # 里的 10刀 是无条件的，不能被后面的拉新词拖累
            referral = bool(re.search(r"拉新|拉人|邀请好友", t[max(0, m.start() - 8):m.start()]))
            if not capped and not referral and (best_plain is None or score > best_plain[0]):
                best_plain = cand
            if referral and (best_ref is None or score > best_ref[0]):
                best_ref = cand
    # 显示优先级：确定值 > 拉新值 > 上限值（分数一律取全体最大，板位不掉）
    pick = best_plain or best_ref
    if pick is not None and pick[2] == best[2]:
        return (best[0], pick[1], pick[2], pick[3])
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


KEY_HINT = re.compile(r"送|赠|免费|额度|刀|亿|token|注册|邀请|有效期|天|次|抽|每天|每周|首充|领"
                     r"|free|credit|trial|giveaway", re.I)   # 英文福利词：中英混排帖的描述行也要能参与排名


def clean_lines(body: str) -> list[str]:
    text = CLEAN_URL.sub(" ", body or "").replace("\u200b", " ")
    out = []
    for p in LINE_SPLIT.split(text):
        p = re.sub(r"\s+", " ", p).strip(" ·|-—…。")
        if len(p) >= 6 and re.search(r"[\u4e00-\u9fffA-Za-z]", p):
            out.append(p)
    return out


DOMAIN_RE = re.compile(r"[a-z0-9-]+\.(?:com|cn|net|ai|io|eu|cc|top|xyz|vip|org|me|dev|pro|surf|tech)", re.I)
BENEFIT_EN = re.compile(r"\b(?:free|token|credit|trial|giveaway|bonus)\b", re.I)


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


CONCRETE_RE = re.compile(r"claude|gpt|gemini|grok|deepseek|glm|qwen|kimi|llama|opus|sonnet|haiku"
                         r"|token|词元|额度|次数|分组|倍率|签到|试用|积分|兑换"
                         r"|图像|视频|生图|画图|模型|接口|api|key|号池|每日|每周", re.I)


def relevant_len(body: str) -> int:
    """与主题相关的正文字数：只统计含中文、≥10 字、且命中福利/模型词表的行。

    免费门槛的"内容丰富"原本看全文字数——omnirush 实测：一整段个人简历把字数堆过线，
    正文真正说福利的就两行。灌水行不含任何模型/额度词，这里直接不计。
    """
    total = 0
    for ln in clean_lines(body):
        if len(ln) < 10 or not re.search(r"[\u4e00-\u9fff]", ln):
            continue
        if KEY_HINT.search(ln) or CONCRETE_RE.search(ln):
            total += len(ln)
    return total


def summarize(body: str, title: str, entry: str = "") -> tuple[list[str], str]:
    lines = clean_lines(body)
    if not lines:
        return ([title.strip()] if title else []), "title"
    # 提到别家域名的行降权：宁可换一句，也别让摘要里的站跟卡片标题对不上
    lines = [ln for ln in lines if not _bare_domain(ln)]
    # 摘要行必须"说得出好处"：中文行要命中发放词/关键词提示，英文行要含福利词。
    # 挡两类实测噪音（t.me/Tkmb678）：门槛句「可能需要0.1刀的验证」（条件已在 reqs 里展示，
    # 且它比描述行短、零命中时按长度排会抢到第一行）和页头残留「Pricing | PayPerQ」。
    # 过滤后一行不剩 → 回退标题（原有行为），不会开天窗。
    lines = [ln for ln in lines
             if (re.search(r"[\u4e00-\u9fff]", ln) and (GIFT_WORD.search(ln) or KEY_HINT.search(ln)))
             or BENEFIT_EN.search(ln)]
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
        score, _, _, _ = amount_of(text)
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
REJECT_REASON: dict[str, str] = {}   # 每个被拒键的原因（健康度日报用，P3）


def build_items(rows: list[dict], cache: dict, now: datetime) -> list[dict]:
    REJECT_REASON.clear()                            # 每轮重置（健康度日报读它）
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
        if champ["url"] not in cache:
            # 正文不在缓存里（CI 兜底只抓得到四源，linux.do 的正文它看不到）→ 无法评判。
            # 键保持不在 PASSED：merge 走快照保留路径。绝不能用标题-only 的证据去重判
            # 本地管道维护的条目——f6236b9 实测：CI 兜底一轮把 22 条剪到 9 条。
            REJECT_REASON[key] = "正文缓存缺失"
            continue

        champ_links = (cache.get(champ["url"]) or {}).get("meta", {}).get("links") or []
        champ_body = (cache.get(champ["url"]) or {}).get("body") or ""
        if len(champ_links) >= 12 or champ_body.count("http") >= 6:
            PASSED[key] = False
            REJECT_REASON[key] = "合集帖"
            continue                                   # 汇总/合集帖：额度属于别家站点，不当一张卡
        body = (cache.get(champ["url"]) or {}).get("body") or ""
        links = list((cache.get(champ["url"]) or {}).get("meta", {}).get("links") or [])
        text = f"{champ.get('title','')}\n{body}"
        score, amount, unit, amount_usd = amount_of(text)
        tags, free = steps_of(text)
        expiry = expiry_of(text)
        entry_pre = pick_entry(key, (champ.get("offer") or {}).get("site") or "", links)
        lines, src = summarize(body, champ.get("title", ""), entry_pre)
        cd_label, cd_deadline = deadline_of(text, champ_time_for_cd := (when(champ) or now))
        entry = entry_pre
        gift_evidence = bool(GIFT_WORD.search(CLEAN_URL.sub(" ", text)))
        # 免费类的专门门槛（用户：尤其免费福利要仔细甄别）：说得清"能拿到什么"才算
        # "具体可得"词表：模型名 + 图像/视频/生图 + 额度/次数/接口 这类能核对的实物
        concrete = bool(CONCRETE_RE.search(text))
        strong_free = bool(re.search(r"免费试用|限时免费|白嫖|0\s*倍率|0倍率|免费使用|免费领|注册即送|无需付费", text))
        valid = relevant_len(body)          # 所有种类都进 ev 快照（复核要用），不只免费类
        hard = "需首充" in tags
        lucky = "需运气" in tags
        paid_verify = bool(re.search(r"[\d.]{1,6}\s*(?:刀|美元|美刀|\$|￥|元)\s*的?\s*(?:验证|押金|保证金|激活)",
                                     text))
        if hard:
            PASSED[key] = False
            REJECT_REASON[key] = "需首充"
            continue                                   # 需充值才能拿的不算"鸡蛋"（那是优惠渠道，另有所属）
        if not gift_evidence:                          # 正文/标题里没有任何发放语境 → 广告或倍率表
            PASSED[key] = False
            REJECT_REASON[key] = "无发放语境"
            continue
        if not amount and not free:                    # 连"免费"都没写、又没数字 → 站点推广，不当鸡蛋
            PASSED[key] = False
            REJECT_REASON[key] = "无额度无免费"
            continue
        if paid_verify and not amount:                 # 先掏钱验证才给用的"免费"不是鸡蛋
            PASSED[key] = False
            REJECT_REASON[key] = "付费验证"
            continue
        if amount_usd is not None and amount_usd < MIN_AMOUNT_USD:
            PASSED[key] = False
            REJECT_REASON[key] = "额度不足$10"
            continue                                   # 币种额度低于下限（midalo 实测：2$ 上板）                                   # （t.me 实测：可能需要0.1刀的验证）

        title_lot = bool(re.search(r"抽奖|开奖|抽码", str(champ.get("title") or "")))
        # 种类看**额度自己身边的词**（±10 字，与 amount_of 的发放词窗口同一口径）：
        #   窗口里有 抽/开奖 → 抽奖卡（「评论抽奖送$500」，$500 是彩注）；
        #   窗口里是 赠/送/领/注册 → 送的卡。docode 实测：标题「注册即送300刀免费额度，每日无条件抽奖」
        #   ——抽奖离 300 刀十几字远，是另一场活动；按整行甚至全帖 lucky 判，就把"送"标成了"抽"。
        #   没有额度时才轮到标题/开奖日（title_lot）。
        amt_win = ""
        if amount:
            t_clean = CLEAN_URL.sub(" ", text)
            pos = t_clean.find(amount)
            if pos < 0:
                # 显示已统一成符号（「300$」），正文里是「300刀」——按数字部分找（防撞上更长数字）
                num = re.sub(r"[^\d.]", "", amount)
                if num:
                    mm = re.search(r"(?<![\d.])" + re.escape(num) + r"(?![\d.])", t_clean)
                    pos = mm.start() if mm else -1
            if pos >= 0:
                amt_win = t_clean[max(0, pos - 10):pos + len(amount) + 10]
        # 窗口里既没有抽奖、也没有发放动词（如 nexotoken 的奖品表「奖品 名额 有效期 $10」），
        # 而全帖在聊抽奖 → 仍按抽奖（保守）。
        has_grant = bool(re.search(r"赠|送|领|注册|体验|试用|免费|解锁", amt_win))
        kind = ("lottery" if (amount and (re.search(r"抽奖|开奖|抽码", amt_win) or (lucky and not has_grant)))
                else "gift" if amount
                else "lottery" if (cd_label == "开奖" or title_lot)
                else "free")
        ease = 2.0 if (free and not hard) else (1.0 if not hard else -3.0)
        ease -= 0.5 * len([t for t in tags if t in ("需回复", "需邀请码", "需拉人", "需签到", "需外网", "限量")])
        ease -= 0.5 * len([t for t in tags if t in ("需邮箱", "需手机号", "需绑TG")])
        if lucky and kind == "lottery":
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
        if kind == "free":
            # 免费类：必须有入口、有发放语境，且"具体可得"（正文≥150 字，或 ≥60 字 + 强发放词）
            rich = valid >= 150 or (valid >= 60 and strong_free)
            if not (entry and gift_evidence and concrete and rich):
                PASSED[key] = False
                REJECT_REASON[key] = "免费门槛"
                continue
        elif total < MIN_SCORE:
            PASSED[key] = False
            REJECT_REASON[key] = "总分不足"
            continue
        PASSED[key] = True
        champ_time = when(champ) or now
        left = LIFE_DAYS - (now.date() - champ_time.date()).days
        if left <= 0:
            REJECT_REASON[key] = "15天到期"
            continue
        if cd_deadline and cd_deadline <= now.timestamp():
            REJECT_REASON[key] = "限时已过"
            continue                                   # 限时已过 → 到点下架（用户口径）
        item = {
            "key": key,
            "brand": (champ.get("offer") or {}).get("brand") or "",
            "amount": ("抽奖 " + amount if kind == "lottery" else amount)
                       or ("抽奖" if kind == "lottery" else
                           "限时免费" if "限时" in expiry else "免费试用" if "试用" in expiry else "免费福利"),
            "kind": kind,
            "unit": unit,
            "score": round(total, 1),
            "level": ("需充值" if hard else "需运气" if (kind == "lottery" and lucky) else
                      "零门槛" if (free and not [t for t in tags if t in ("需回复", "需邀请码", "需签到", "需拉人", "需手机号", "需绑TG", "需外网")])
                      else "低门槛"),
            "level_class": ("hard" if hard else "luck" if (kind == "lottery" and lucky) else
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
            "ev": {"body": len(body), "valid": valid, "concrete": concrete,
                   "strong_free": strong_free, "entry": entry},
        }
        items.append(item)
    items.sort(key=lambda x: -x["t"])
    return items[:MAX_ITEMS] if len(items) > MAX_ITEMS else items


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
                    body_len = ev["valid"] if "valid" in ev else ev.get("body", 0)
                    rich_old = body_len >= 150 or (body_len >= 60 and ev.get("strong_free"))
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
