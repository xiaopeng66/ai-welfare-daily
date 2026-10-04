#!/usr/bin/env python3
"""Parser-level guards for fetch.py.

Why this exists: test_guards injects every source function, so not a single
parser or regex in fetch.py is ever executed by it - which is how a broken URL,
a mis-paired timestamp and an order-sensitive meta regex all survived a green
suite. Every fixture below is the markup shape a real defect was found in, so
the same defect cannot come back quietly.

Usage:
    python3 tests/test_parsers.py [path/to/fetch.py]

Hermetic: parses strings, never touches the network.
"""
import importlib.util
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FETCH_PY = os.environ.get('FETCH_PY') or (
    sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'fetch.py')
)

spec = importlib.util.spec_from_file_location('fetcher', FETCH_PY)
assert spec and spec.loader, f'cannot load {FETCH_PY}'
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

results = []


def check(ok, msg, extra=None):
    results.append(bool(ok))
    detail = '' if extra is None else f' :: {extra}'
    print(('ok    ' if ok else 'FAIL  ') + msg + (detail if not ok else ''))


# --------------------------------------------------------------------------
print('== linux.sb listing: each row owns its own timestamp ==')
# Two ordinary rows: the anchor carries the title, the row's own <span> the time.
page = (
    '<div class="post-title-row"><a class="post-title" href="/topic/111">鸡蛋 大放送</a></div>'
    '<span data-performance-time="1700000000">刚刚</span>'
    '<div class="post-title-row"><a class="post-title" href="/topic/222">中转站 体验金</a></div>'
    '<span data-performance-time="1700009999">1 小时前</span>'
)
rows = mod.parse_linuxsb_listing(page)
check(len(rows) == 2, 'two rows parsed', len(rows))
check([r['id'] for r in rows] == ['111', '222'], 'ids in document order', [r['id'] for r in rows])
check(rows[0]['created_at'] and rows[1]['created_at']
      and rows[0]['created_at'] < rows[1]['created_at'],
      'each row keeps its own time', [r['created_at'] for r in rows])
check(rows[0]['url'] == f'{mod.BASE_LINUXSB}/topic/111', 'url built from the id', rows[0]['url'])

print('\n== a row that lost its <span> must not eat its neighbour ==')
# Regression: the old whole-page regex used `.*?` across row boundaries, so this
# page yielded ONE row (222 with 111's timestamp stolen) instead of two.
page = (
    '<div class="post-title-row"><a class="post-title" href="/topic/111">鸡蛋 大放送</a></div>'
    '<div class="post-title-row"><a class="post-title" href="/topic/222">中转站 体验金</a></div>'
    '<span data-performance-time="1700009999">1 小时前</span>'
)
rows = mod.parse_linuxsb_listing(page)
check(len(rows) == 2, 'the following row is still parsed', len(rows))
check(rows[0]['created_at'] is None, 'the anomalous row gets no timestamp', rows[0]['created_at'])
check(rows[1]['created_at'] is not None and rows[1]['created_at'].startswith('2023-11-1'),
      'and its neighbour keeps its own', rows[1]['created_at'])

print('\n== the daily-hot block nests a reply count inside the anchor ==')
page = (
    '<li><a href="/topic/333"><span class="daily-hot-topics-content">'
    '<span class="daily-hot-topics-title">免费订阅</span>'
    '<span class="daily-hot-topics-count">近 24 小时 29 回复</span></span></a></li>'
)
rows = mod.parse_linuxsb_listing(page)
check(len(rows) == 1 and rows[0]['title'] == '免费订阅',
      'title taken from the title element, not the whole anchor',
      rows[0]['title'] if rows else 'no row')

print('\n== a pagination link inside a row is not a second row ==')
page = (
    '<div class="post-title-row"><a class="post-title" href="/topic/555">鸡蛋 大放送</a>'
    '<span class="topic-pages"><a href="/topic/555?p=2">2</a></span></div>'
    '<span data-performance-time="1700000000">刚刚</span>'
)
rows = mod.parse_linuxsb_listing(page)
check(len(rows) == 1 and rows[0]['id'] == '555',
      'the ?p=2 link is not a topic', [r['id'] for r in rows])

print('\n== baipiao HTML fallback builds a real topic url ==')
rows = mod.parse_topics('<a href="/bbs/d/777">求一个中转站</a>', mod.BaipiaoHTMLParser)
check(len(rows) == 1, 'one row', len(rows))
check(rows[0]['url'] == 'https://baipiao.org/bbs/d/777',
      'url is the topic page, not https://baipiao.org777', rows[0]['url'])
check(mod._site_of(rows[0]['url']) == 'baipiao.org',
      'and its host matches the JSON API path, so the two never duplicate')
check(mod.topic_key(rows[0]) == mod.topic_key(
    {'id': '777', 'url': 'https://baipiao.org/bbs/d/777'}),
    'fallback row and API row share one merge key')

print('\n== nodeloc listing urls ==')
rows = mod.parse_topics('<a href="/t/topic/999">公益站 体验金</a>', mod.NodeLocHTMLParser)
check(len(rows) == 1 and rows[0]['url'] == f'{mod.BASE_NODELOC}/t/topic/999',
      'url built from the id', rows[0]['url'] if rows else 'no row')
check(rows and mod._site_of(rows[0]['url']) == 'nodeloc.com',
      'and its host normalizes to a single namespace (www. stripped)',
      mod._site_of(rows[0]['url']) if rows else 'no row')

print('\n== publish time extraction accepts either attribute order ==')
property_first = ('<meta property="article:published_time" content="2026-09-30T12:00:00+08:00">')
content_first = ('<meta content="2026-09-30T12:00:00+08:00" property="article:published_time">')
for label, html in (('property first', property_first), ('content first', content_first)):
    got = mod.extract_published_time(html)
    check(got == '2026-09-30T04:00:00+00:00', f'{label} -> utc iso', got)
check(mod.extract_published_time('<meta property="og:title" content="x">') is None,
      'no meta tag -> None')
check(mod.extract_published_time(
    '<meta property="article:published_time" content="not-a-date">') is None,
    'garbage date -> None instead of raising')

print('\n== topic_key needs a usable namespace even without a url ==')
k = mod.topic_key({'id': '42', 'url': None, 'source': 'nodeloc'})
check(k == 'nodeloc#42', 'a url-less row falls back to its source namespace', k)
two = mod.deduplicate([
    {'id': '42', 'url': None, 'source': 'nodeloc', 'title': 'A'},
    {'id': '42', 'url': None, 'source': 'linuxdo_welfare', 'title': 'B'},
])
check(len(two) == 2, 'two url-less rows from different sites both survive dedupe',
      [t['source'] for t in two])
check(mod.topic_key({'id': '42', 'url': 'https://linux.sb/topic/42'}) == 'linux.sb#42',
      'a normal row is namespaced by host')
check(mod.topic_key({'id': '42', 'url': 'https://www.linux.sb/topic/42'}) == 'linux.sb#42',
      'www. is not a separate site')


print('\n== published_verified must mean "this is the publish time" ==')


def with_mocked_fetch(payload, fn):
    """Run a fetch_* function against a canned API payload."""
    real = mod.fetch
    mod.fetch = lambda *a, **k: json.dumps(payload)
    try:
        return fn()
    finally:
        mod.fetch = real


topics_json = {'topic_list': {'topics': [
    {'id': 77, 'title': '公益站 体验金', 'bumped_at': '2026-10-01T09:00:00.000Z'},
    {'id': 78, 'title': '中转站 额度', 'created_at': '2026-10-01T09:00:00.000Z'},
]}}
rows = {r['id']: r for r in with_mocked_fetch(topics_json, mod.fetch_nodeloc)}
check(rows.get('77', {}).get('created_at') == '2026-10-01T09:00:00.000Z',
      'a topic without created_at still gets a usable timestamp',
      rows.get('77', {}).get('created_at'))
check(not rows.get('77', {}).get('published_verified'),
      'but a reply time is not claimed as a verified publish time',
      rows.get('77', {}).get('published_verified'))
check(rows.get('78', {}).get('published_verified'),
      'created_at is a verified publish time', rows.get('78', {}).get('published_verified'))


print('\n== vibex (心流AI社区) welfare board ==')
vibex_json = {'topic_list': {'topics': [
    {'id': 5, 'title': '关于“iFlow 补给站”类别', 'pinned': True,
     'created_at': '2025-08-23T02:00:00.000Z'},
    {'id': 6773, 'title': '免费公益站 GPT+Claude Code 每日登录送25刀',
     'created_at': '2026-10-01T07:10:00.000Z'},
    {'id': 6770, 'title': '领鸡蛋 DeepSeek官方客户端',
     'bumped_at': '2026-10-02T03:00:00.000Z'},
]}}


def with_paged_fetch(pages, fn):
    """Mock fetch so each page argument gets its own payload.

    The single-payload mock above is fine for a one-page function, but vibex
    walks pages 1-3: handing every page the same body makes the last page's row
    overwrite the first one's source, which looks like a source-naming bug when
    it is really the fixture not modelling pagination.
    """
    real = mod.fetch

    def fake(url, *a, **k):
        m = re.search(r'page=(\d+)', url)
        page = int(m.group(1)) if m else 1
        return json.dumps(pages.get(page, {'topic_list': {'topics': []}}))

    mod.fetch = fake
    try:
        return fn()
    finally:
        mod.fetch = real


vrows = {r['id']: r for r in with_paged_fetch({1: vibex_json}, mod.fetch_vibex_welfare)}
check('5' not in vrows, 'the pinned board description is not a welfare post', sorted(vrows))
check(vrows.get('6773', {}).get('url') == 'https://vibex.iflow.cn/t/topic/6773',
      'url built from BASE_VIBEX', vrows.get('6773', {}).get('url'))
check(vrows.get('6773', {}).get('source') == 'vibex_welfare_p1',
      'source is namespaced per page', vrows.get('6773', {}).get('source'))
check(vrows.get('6773', {}).get('published_verified'),
      'created_at is a verified publish time', vrows.get('6773', {}).get('published_verified'))
check(vrows.get('6770', {}).get('created_at') == '2026-10-02T03:00:00.000Z'
      and not vrows.get('6770', {}).get('published_verified'),
      'a bumped_at fallback is usable but not claimed as verified',
      (vrows.get('6770', {}).get('created_at'), vrows.get('6770', {}).get('published_verified')))
check(set(r['source'] for r in with_paged_fetch(
          {1: {'topic_list': {'topics': [{'id': 1, 'title': 'a', 'created_at': '2026-01-01T00:00:00.000Z'}]}},
           2: {'topic_list': {'topics': [{'id': 2, 'title': 'b', 'created_at': '2026-01-02T00:00:00.000Z'}]}},
           3: {'topic_list': {'topics': [{'id': 3, 'title': 'c', 'created_at': '2026-01-03T00:00:00.000Z'}]}},
          }, mod.fetch_vibex_welfare)) == {'vibex_welfare_p1', 'vibex_welfare_p2', 'vibex_welfare_p3'},
      'each page keeps its own source label')


# --------------------------------------------------------------------------
# Relevance gate (two-tier) and 公益站 tagging.
#
# Why the gate is two-tier instead of one keyword list: measured on the full
# 2026-10-04 corpus (607 fetched / 509 unique titles) the single-layer gate had
# two opposite defects -- it dropped 194 AI-额度 posts (「注册送1刀」「0.0001倍率」
# 「放粮」「号池」) because the shop talk contains none of the 8 gate words, and it
# kept 24 non-AI ones, ALL from 「抽奖」 (VPS/esim/TG号/域名/会员 lotteries).
# Both directions are pinned below, so a future "simplify the filter" edit that
# reintroduces either one fails here instead of quietly on the live site.
# --------------------------------------------------------------------------
def relevant(title):
    return mod.is_relevant_title(title)


# ① Self-sufficient terms: enough on their own (they *are* an AI welfare site
#    or an explicit quota giveaway), so they must survive without any other hit.
for title in ['🥚【露娜半公益中转站】🥚 都是免费登！',
              '（公益生图站）发一些兑换码',
              'Zynk公益复活! 进来兑换额度',
              '【Zynk 公益】国庆第二波福利，GPT 6.1 Sol 蹬 $1000',
              '🆕 无限deepseek 持续公益 已送【2.5】亿',
              '国庆福利 纯grok heavy号池',
              'token 多到溢出拿來燒水,滞销!今天開門放糧！']:
    check(relevant(title), 'a self-sufficient title passes the relevance gate', title)

check('公益站' in mod.score_topic({'title': '🆕 无限deepseek 持续公益 已送【2.5】亿'})['tags'],
      'a bare-公益 title is tagged into the 公益站 category',
      mod.score_topic({'title': '🆕 无限deepseek 持续公益 已送【2.5】亿'})['tags'])

# ② Domain ∧ Offer: the actual 中转站 shop talk, which the old gate missed
#    because it names no gate word -- it says 「注册送N刀」/「倍率」/「签到」.
for title in ['0.0001倍率 注册送一刀 还可以签到',
              '0.5倍率的claude max！注册送1刀！回复id再送5刀',
              '0.04超低倍率GPT 5.6，注册就送1刀，还能每日签到',
              '领10刀0.4xgpt谷歌登录',
              '织云站点 限时开放注册 赠送200刀 可签到',
              'LongCat邀请新用户实名，各得1000万Tokens！拼团返现']:
    check(relevant(title), 'domain AND offer talk passes the relevance gate', title)

# ③ A domain term ALONE is not a freebie ("Claude 注册防封经验" is a how-to).
for title in ['Claude 注册防封经验：这些细节真的容易翻车',
              '快用不起了，ds涨价后，go的额度也下降了']:
    check(not relevant(title), 'a domain term without a giveaway still fails', title)

# ④ An offer ALONE is not AI (this is the 「抽奖」 noise that used to leak).
for title in ['【抽奖】十一快乐月付香港小鸡一台',
              '[已开奖]抽20台香港nat小鸡 国际精品BGP|大带宽|原生IP全解锁',
              '[已开奖]「抽奖」一张中港澳1G流量esim，钞我🥵',
              '[已开奖]【抽奖】临期域名一枚（sss.kim）',
              '[已开奖]【抽奖】哔哩哔哩大会员月卡',
              '[已开奖]抽奖tg号 直接30分钟开奖',
              '抽奖，9HTTP代理IP送点动态住宅代理',
              '[已开奖]Gridly Mac 下的轻巧的窗口管理软件抽奖啦',
              '感谢饼友打赏，抽奖送称号（已经全部发放完成）']:
    check(not relevant(title), 'a non-AI lottery fails the relevance gate', title)

# ⑤ The old noise floor must stay rejected (免费 is not a domain term on its own).
for title in ['大毛大毛！！支付宝 微信境外支付参加活动利润最低40+ 速撸',
              'VMISS 薅羊无保姆级教程：支付宝「境外支付笔笔减」',
              '微信支付宝境外支付有礼',
              '分享免费苹果共享ID网站',
              '天翼云手机2天卡，可无限续杯～']:
    check(not relevant(title), 'an unrelated 羊毛 post still fails the gate', title)

# ⑥ CJK-adjacent model names: r"\bgpt\b" does NOT match 「月gpt」 because Python
#    treats 月 as a word char, so the gate uses ASCII lookarounds instead.
check(relevant('【抽奖】吐血福利免费送4个月gpt额度抽奖'),
      'a model name glued to Chinese still matches (ASCII lookaround)',
      '月gpt')
check(relevant('GPT6免费瞪？！') and relevant('GLM-5.3 已上线可获得 2000万Tokens'),
      'model names with a version suffix still match (GPT6 / GLM-5.3)')
# 直接钉住边界规则本身。上面那些「整句过闸」的断言钉不住它：`白嫖福利，富哥请吃
# deepseekv4flash` 即使模型名匹配不上也有 `白嫖` 兜底，会**因为错误的理由通过**
# （变异测试就是这么发现的）。所以这里直接查 DOMAIN 正则。
for probe in ['deepseekv4flash', 'DeepSeekharness', 'opus5', 'GPT6', 'GLM-5.3', '月gpt']:
    check(mod._DOMAIN_RE.search(probe) is not None,
          'a model name with a glued version/suffix is matched by the domain regex', probe)
for probe in ['keyboard', 'aidata', 'cursors']:
    check(mod._DOMAIN_RE.search(probe) is None,
          'a short ASCII keyword does not match inside another word', probe)
# 但「型号词 + 无福利信号」不算：这是发帖人在描述自己用的模型，不是在放额度。
check(not relevant('0.01x DeepSeek 不降智不掺水'),
      'a model name alone (no giveaway signal) does not pass', '0.01x DeepSeek')

# ⑦ 黑话字：只写「抽奖」会漏掉「先抽个奖叭」「抽50个10¥余额」（中间隔了字），
#    所以 offer 信号收单字「抽」；「蹬」= 免费用额度，「国模」= 国产模型。
#    这三个词是实测补的：全量语料上净增 11 条真货、0 条噪音。
for title in ['橘-API  token滞销，帮帮我们！！！ 事已至此先抽个奖叭  第一名依旧140刀！',
              '【抽50个10¥余额】Air Router中转-价格稳定智商兼顾：plus0.1x',
              '【第3波】欢度国庆，爽蹬$1000刀🔥🔥🔥',
              '【猛蹬】claude顶级模型不花钱！',
              '新站开业，国模免费用',
              '【第4波】国庆节快乐 GPT6系列 速蹬']:
    check(relevant(title), 'shop-talk (抽/蹬/国模) still passes the gate', title)
# 但源站自己标了「无了」的就不是活福利了（同一批标题里的兄弟帖，已发完）。
check(not relevant('【无了】国庆节快乐 GPT6系列 速蹬'),
      'the same title marked 无了 is dropped as finished')

# ⑬「发」不能裸用：它会命中 开发 / 发现 / 发布。但删掉它又丢真货（`又发1亿token`、
#    `发鸡蛋啦`、`发点999刀CDK` 共 8 条），所以只挡明确的非发放搭配。
#    `发现1不错GPT中转` 没有任何其它发放信号 —— 修复前它全靠「发」混进来，现在该被挡。
check(not relevant('报! 发现1不错GPT,Grok中转'),
      'a title whose only giveaway-ish word is 发现 must not pass')
# 而 `发现一免费 DeepSeek` 是靠「免费」成立的（真有人发了个免费的），照收。
check(relevant('发现一免费 DeepSeek v4 flash'),
      'a 发现 title that really is a freebie still passes (on 免费)')
for title in ['deepseek发鸡蛋啦，登录dsh客户端就有',
              '【RelayFor】发点999刀CDK',
              '[臭鸡蛋]zcode 9 月 30 又发 1 亿 token']:
    check(relevant(title), 'but a real 发放 still passes', title)
# 「低价」是独立的发放信号：这条帖不靠「发」也成立。
check(relevant('发布会前最后一份低价20x。只有老号才有的额度'),
      'a low-price post passes on 低价 regardless of 发')

# ⑧ 但放开单字「抽」后，非 AI 的抽奖必须仍然被挡（AND 判定兜住）。
for title in ['抽奖，9HTTP代理IP送点动态住宅代理',
              '[已开奖]抽20台香港nat小鸡 国际精品BGP|大带宽|原生IP全解锁']:
    check(not relevant(title), 'single-char 抽 does not let non-AI lotteries back in', title)

# ⑨ 标的物维度：收的东西不是 AI 用量 → 剔除。这是「送点积分」「抽个 TG 号」
#    「白嫖一台小鸡」过去能混进来的根因 —— 此前判定只问「出现了什么词」，
#    从不过问送的是什么，于是「送 10 刀额度」和「送点论坛积分」结构上完全等价。
#    下面 12 条全是实测从 store 里剔掉的噪音。
for title in ['今天风太大，不能出去玩了给饼饼送点积分玩玩',
              '积分抽奖中奖概率大幅降低了？',
              '发点积分，各位国庆节快乐呀',
              '俩UR都有了，接下来是继续抽奖还是囤积分啊',
              '这一周有其他事情要做，可能不发帖的，发点积分吧',
              '创作者通过，发点积分',
              '抽两个富可敌国称号+800积分百连抽一次！',
              '院长的公益节点在何处能找到呢',
              '宝可梦机场之十月庆典免费兑换码之猜猜我是谁',
              '白嫖Stripe500$信用额度',
              '【免费 NAT 小鸡 + 家宽出口】一台白嫖 VPS 挂纯净住宅 IP：ChatGPT 不降智',
              '【OK24shop】steam充值卡100泰铢兑换成功！现在有效',
              '话费充值9折优惠，全国通用',
              'X会员低价充值 3个月 20  6个月40']:
    check(not relevant(title), 'a non-AI giveaway target fails the gate', title)
# 「积分」被移出领域词，正是这批闲聊过闸的原因（它在论坛语境里指的是论坛积分）。
check('积分' not in mod.DOMAIN_KEYWORDS,
      '积分 is not an AI domain term (forum points are not relay credit)')
# 同理「充值」：它是**行为信号**（在 OFFER 里），放领域词里会让非 AI 充值帖过关
# （实测移出后语料只掉 1 条，而那条正是 `X会员低价充值`）。
check('充值' not in mod.DOMAIN_KEYWORDS and '充值' in mod.OFFER_KEYWORDS,
      '充值 is a giveaway signal, not an AI domain term')

# ⑩ 会员类：ChatGPT Plus / Gemini 会员是账号商品，默认否决；但站点自己的充值优惠
#    （价格词 + AI 词同现）属于目标内容，必须放行。用户 2026-10-04 决定：会员类放行。
for title in ['Google AI Pro 又送一年会员 这是我在推特看到的，',
              '不但可以领到大额余额，还可以用余额体验SVIP',
              '【抽奖】吐血福利免费送4个月gpt plus会员抽奖']:
    check(not relevant(title), 'a membership giveaway without a price deal is not a relay post', title)
check(relevant('【明天结束】CUN.AI 首充 $66 拿 Claude / GPT Max 约 3.3 折，会员返利还叠加！'),
      'a station price deal survives the membership veto')

# ⑪ 已结束的帖子不留：福利已开奖/领完/失效，留着就是死信息（实测积了 24 条）。
#    用户 2026-10-04 决定「已结束就剔除，下次更新时删掉」。
for title in ['[已开奖]token 多到溢出拿來燒水,滞销!今天開門放糧！',
              '[已开奖]自建中转站抽奖',
              '【10-1】欢度国庆①，爽蹬$1000刀(已完)',
              '【福利已无】快来蹬基元律动 10/7到期',
              '【已失效等恢复】白嫖Claude！',
              '赠送ralayfor公益站注册码一枚（已赠送）']:
    check(not relevant(title), 'an ended post is rejected even when the topic is on point', title)

# ⑫ 本轮补的词：薅 / 邀请 / 返现 / 不花钱，以及「模型名直接粘字母」的写法
#    （`deepseekv4flash` / `DeepSeekharness` 用 _prefix_bounded 才命中）。
for title in ['个人一直在薅的羊毛（Claude、GPT、DeepSeek都有），分享给兄弟们',
              '分享一个能薅ds api 羊毛的网址',
              'LongCat邀请新用户实名，各得1000万Tokens！拼团返现，最高返50%！',
              '【猛蹬】claude顶级模型不花钱！',
              '白嫖福利，富哥请吃deepseekv4flash',
              '登录 DeepSeekharness 桌面版，领 6 元赠金',
              # 纯价格表帖：一个 giveaway 词都没有，靠本轮加的「充值/起充」类信号进来，
              # 正是用户要的「优惠渠道」内容。
              '[NachoNekoAPI] 0.01x DeepSeek 不降智不掺水 1:1充值',
              '【烧饼换用量】Zynk API (Paid) | 国模 0.25x, Claude 0.09x起, GPT 0.07x起 | 一元起充']:
    check(relevant(title), 'the words added this round admit their own real posts', title)

# ⑬ 新增分类「优惠渠道」：低价/优惠的中转渠道单独成类（用户 2026-10-04 要求）。
check('优惠渠道' in mod.score_topic({'title': '0.04超低倍率GPT 5.6，注册就送1刀'})['tags'],
      'a low-price relay post is tagged 优惠渠道',
      mod.score_topic({'title': '0.04超低倍率GPT 5.6，注册就送1刀'})['tags'])
# 报价的主流写法是 `0.09x`，词表抓不住，所以分类有「形状」补充（CATEGORY_PATTERNS）。
# 这条标题里**没有任何价格词**（无「折/倍率/优惠/充值」），标签只能来自正则。
check('优惠渠道' in mod.score_topic({'title': '掺水司马API GPT直KEY现在0.35x'})['tags'],
      'a bare 0.35x price (no price word at all) is tagged 优惠渠道',
      mod.score_topic({'title': '掺水司马API GPT直KEY现在0.35x'})['tags'])
# 但必须带小数点：裸 `3x` 会撞上 `3x-ui` 面板，`20x` 会撞上 Claude 订阅档位。
check('优惠渠道' not in mod.score_topic({'title': '按教程搭建 3x-ui 系统和节点'})['tags'],
      'a bare 3x (the 3x-ui panel) is not a price tag')

# ⑭ cap 300 是行为契约的一部分：闸门放宽后每轮入库量翻倍，回到 200 会立刻
#    把窗口从 65 天压到 11 天并让 vibex 整源归零（2026-10-04 实测）。
check(mod.DEFAULT_LIMIT >= 300,
      'DEFAULT_LIMIT stays large enough to keep every source in the window',
      f'DEFAULT_LIMIT={mod.DEFAULT_LIMIT}')

passed = sum(results)
print(f'\n{passed}/{len(results)} checks passed')
sys.exit(0 if passed == len(results) else 1)
