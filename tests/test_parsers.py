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
# Relevance gate and 公益站 tagging.
#
# Why: the gate runs BEFORE score_topic, so "公益" must be in RELEVANCE_KEYWORDS
# for a bare-公益 title to survive long enough to be tagged 公益站. When it was
# missing, titles like "【Zynk 公益】…" and "（公益生图站）发一些兑换码" were dropped
# even though CATEGORY_KEYWORDS already knew how to tag them.
# --------------------------------------------------------------------------
def relevant(title):
    return any(kw.lower() in title.lower() for kw in mod.RELEVANCE_KEYWORDS)


for title in ['🥚【露娜半公益中转站】🥚 都是免费登！',
              '（公益生图站）发一些兑换码',
              'Zynk公益复活! 进来兑换额度',
              '【Zynk 公益】国庆第二波福利，GPT 6.1 Sol 蹬 $1000',
              '🆕 无限deepseek 持续公益 已送【2.5】亿']:
    check(relevant(title), 'a bare-公益 title passes the relevance gate', title)

check('公益站' in mod.score_topic({'title': '🆕 无限deepseek 持续公益 已送【2.5】亿'})['tags'],
      'a bare-公益 title is tagged into the 公益站 category',
      mod.score_topic({'title': '🆕 无限deepseek 持续公益 已送【2.5】亿'})['tags'])

# The gate must stay narrow: adding "公益" is not an invitation to let the
# non-AI 羊毛 posts through, which is exactly why "免费" stays category-only.
for title in ['大毛大毛！！支付宝 微信境外支付参加活动利润最低40+ 速撸',
              'VMISS 薅羊无保姆级教程：支付宝「境外支付笔笔减」',
              '微信支付宝境外支付有礼',
              '分享免费苹果共享ID网站',
              '天翼云手机2天卡，可无限续杯～']:
    check(not relevant(title), 'an unrelated 羊毛 post still fails the gate', title)

passed = sum(results)
print(f'\n{passed}/{len(results)} checks passed')
sys.exit(0 if passed == len(results) else 1)
