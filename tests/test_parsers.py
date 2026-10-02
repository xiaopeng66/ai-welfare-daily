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
import os
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

passed = sum(results)
print(f'\n{passed}/{len(results)} checks passed')
sys.exit(0 if passed == len(results) else 1)
