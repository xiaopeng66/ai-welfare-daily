#!/usr/bin/env python3
"""Guard the published page: it must be a pure function of the store.

The site is rebuilt on every scheduled run. Anything in the render that depends
on the wall clock makes every run produce a fresh file, so the workflow commits
it and Pages redeploys - a commit every few hours even when no post is new.
These checks pin down the properties that keep that from creeping back.

Usage: python3 tests/test_site.py
"""
import hashlib
import json
import os
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN_PY = os.path.join(ROOT, 'generate.py')
STORE = os.path.join(ROOT, 'data', 'topics.jsonl')
PAGE = os.path.join(ROOT, 'docs', 'index.html')

results = []


def check(ok, msg, extra=None):
    results.append(bool(ok))
    detail = '' if extra is None else f' :: {extra}'
    print(('ok    ' if ok else 'FAIL  ') + msg + (detail if not ok else ''))


def digest(b):
    return hashlib.md5(b).hexdigest()


def row(tid, title, created, fetched, source='linuxsb_p1', **kw):
    r = {'id': str(tid), 'title': title, 'url': f'https://linux.sb/topic/{tid}',
         'source': source, 'tags': [], 'score': 0, 'created_at': created,
         'fetched_at': fetched, 'published_verified': True}
    r.update(kw)
    return r


def build(rows, out, store):
    with open(store, 'w', encoding='utf-8') as f:
        f.write(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    return subprocess.run([sys.executable, GEN_PY, '-i', store, '-o', out],
                          capture_output=True, text=True)


def cards_of(html):
    """Parse the payload the page embeds as a JS array literal.

    raw_decode rather than a regex: the payload is one big JSON array and the
    tests need its exact extent (also used for the escaping check below).
    """
    i = html.index('const cards=')
    start = html.index('[', i)
    cards, end = json.JSONDecoder().raw_decode(html[start:])
    return cards, html[start:start + end]


d = tempfile.mkdtemp(prefix='site-test-')
store, out = os.path.join(d, 'topics.jsonl'), os.path.join(d, 'index.html')

print('== the "最近收录" badge comes from the data, not the clock ==')
# The v6 page computes the badge client-side from max(fetched_at), so the
# rendered HTML must NOT bake in any wall-clock value, and the badge code must
# read the fetched_at of the newest row. A row last changed in 2020 + a page
# containing "now" would mean the clock crept back into the render.
p = build([row(9001, '额度 A', '2019-12-31T20:00:00Z', '2020-01-01T00:00:00Z')], out, store)
check(p.returncode == 0, 'generate.py exits 0', p.stderr[-300:])
html = open(out, encoding='utf-8').read()
check('2020-01-01T00:00:00Z' in html,
      'the newest row (and its fetched_at) is embedded', None)
check(not re.search(r'最近收录[^<]*\d{4}-\d{2}-\d{2}', html),
      'no wall-clock badge is baked into the HTML', None)
check("Math.max(0,...cards.map(c=>stamp(c.fetched_at)))" in html,
      'the badge is computed client-side from max(fetched_at)', None)

print('\n== rendering the same store twice is a no-op ==')
rows = [row(1, '额度 A', '2026-09-30T10:00:00Z', '2026-10-01T12:00:00Z'),
        row(2, '抽奖 </script><img src=x onerror=alert(1)>', '2026-10-01T10:00:00Z',
            '2026-10-01T12:00:00Z'),
        row(3, '体验金 C', None, '2026-10-01T12:00:00Z')]
p1 = build(rows, out, store)
first = open(out, 'rb').read()
mtime = os.path.getmtime(out)
p2 = build(rows, out, store)
second = open(out, 'rb').read()
check(digest(first) == digest(second), 'byte-identical output', f'{digest(first)} vs {digest(second)}')
check(os.path.getmtime(out) == mtime, 'the file is not even rewritten')
check('unchanged' in p2.stderr or 'up to date' in p2.stderr,
      'the log says the page did not change', p2.stderr.strip().splitlines()[-1:])

print('\n== payload integrity ==')
html = open(out, encoding='utf-8').read()
cards, payload = cards_of(html)
check(len(cards) == 3, 'every stored row is rendered', len(cards))
check(all(c['id'] for c in cards), 'no row lost its id')
check('<' not in payload,
      'the embedded JSON contains no raw < (a scraped "</script>" cannot break out)')
check('</script><img' not in html, 'a hostile title does not reach the DOM as markup')
check(cards[2]['created_at'] is None and cards[2]['id'] == '3',
      'a row with no created_at still renders (the badge falls back to fetched_at)')

print('\n== the committed page is what the committed store renders to ==')
if os.path.exists(STORE) and os.path.exists(PAGE):
    tmp = os.path.join(d, 'committed.html')
    subprocess.run([sys.executable, GEN_PY, '-i', STORE, '-o', tmp], capture_output=True, text=True)
    a, b = open(tmp, 'rb').read(), open(PAGE, 'rb').read()
    check(a == b, 'docs/index.html is up to date with data/topics.jsonl',
          f'{digest(a)} != {digest(b)} - regenerate and commit the page')
else:
    check(False, 'store and page are present in the repo')

shutil.rmtree(d, ignore_errors=True)

print('\n== every category has a tone, an order slot, and a CSS rule ==')
# fetch.py 是分类的权威来源（打标用的是它），generate.py 只负责展示。
# v6 模板把展示映射从「图标+CSS」换成「色调表 tones + 顺序表 order」，断言跟着搬：
# 只要分类词表里有它，注入模板的三张表就必须有 —— 漏掉只会让按钮/标签缺色，不报错。
_s2 = importlib.util.spec_from_file_location('gen_mod', os.path.join(ROOT, 'generate.py'))
_g = importlib.util.module_from_spec(_s2)
_s2.loader.exec_module(_g)
_f2 = importlib.util.spec_from_file_location('fetch_mod', os.path.join(ROOT, 'fetch.py'))
_f = importlib.util.module_from_spec(_f2)
_f2.loader.exec_module(_f)
for cat in _f.CATEGORY_KEYWORDS:
    check(cat in _g.CATEGORY_TONES, 'a category in the map has a tone (else its tag is bare)', cat)
    check(cat in _g.CATEGORY_ORDER, 'and it is in the display order (else it sorts to the end)', cat)
# 注入后的页面里：tones/order 表和 CSS 色规则都必须真实落在 index.html 上
_site_html = open(os.path.join(ROOT, 'docs', 'index.html'), encoding='utf-8').read()
_tones = re.search(r'const tones=(\{[^}]*\});', _site_html)
_order = re.search(r'const order=(\[[^]]*\]);', _site_html)
check(_tones is not None and _order is not None,
      'the template injection points survive rendering', None)
if _tones and _order:
    tones_js = _tones.group(1)
    order_js = _order.group(1)
    for cat in _f.CATEGORY_KEYWORDS:
        check(f'"{cat}"' in tones_js, 'the rendered tones map carries every category', cat)
        check(f'"{cat}"' in order_js, 'the rendered order carries every category', cat)
    check(f'.tag[data-tone=' in _site_html or 'data-tone=' in _site_html,
          'the page styles tags by tone', None)
# 顺序本身：用户要求 抽奖 排在 优惠渠道 之前。
check(_g.CATEGORY_ORDER.index('抽奖') < _g.CATEGORY_ORDER.index('优惠渠道'),
      '抽奖 sorts before 优惠渠道 (swapped on user request)',
      _g.CATEGORY_ORDER)

print('\n== filter buttons are derived from the data at runtime ==')
# v6 的来源/分类按钮完全由页内 JS 从 cards 派生（写死按钮的两个旧 bug 从模板层
# 就不可能再发生），所以断言盯的是派生逻辑与注入的映射表：
# 1) labels 表注入了全部五个源；2) sourceKey 做前缀匹配（任意新源不静默丢弃）；
# 3) categories 由 order + 数据中出现的新值拼接；4) 源过滤按前缀匹配而非白名单。
d2 = tempfile.mkdtemp(prefix='site-filters-')
store2, out2 = os.path.join(d2, 'topics.jsonl'), os.path.join(d2, 'index.html')
rows2 = [row(1, '额度 A', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', source='linuxsb_p1'),
         row(2, '额度 B', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z',
             source='vibex_welfare_p1', tags=['优惠渠道'], score=1),
         row(3, '额度 C', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z',
             source='somebrand_new_board', tags=['免费放粮'], score=1)]
build(rows2, out2, store2)
h2 = open(out2, encoding='utf-8').read()
labels = re.search(r'const labels=(\{[^}]*\});', h2)
check(labels is not None, 'the labels map is injected', None)
if labels:
    for src in ['linuxsb', 'baipiao', 'nodeloc', 'linuxdo', 'vibex', 'nextbuf']:
        check(f'"{src}":' in labels.group(1), 'labels knows every production source', src)
check('const sourceKey=s=>Object.keys(labels).find(k=>String(s||' in h2,
      'sourceKey matches any prefix (new sources are not silently dropped)', None)
check('[...order.filter(c=>present.includes(c)),...present.filter(c=>!order.includes(c))]' in h2,
      'category chips = known order first, then any new category from the data', None)
check('sourceKey(c.source)===state.source' in h2,
      'the source filter matches by key, not a hardcoded if-chain', None)
check("sourceFilter==='linuxsb'" not in h2 and "sourceFilter==='nodeloc'" not in h2,
      'the per-source if-chain is gone', None)


