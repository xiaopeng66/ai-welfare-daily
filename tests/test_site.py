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

print('== the "更新于" badge comes from the data, not the clock ==')
# A row last changed in 2020 can only produce a 2020 badge if the stamp is read
# from the store -- a rendered-at timestamp would print today.
p = build([row(9001, '额度 A', '2019-12-31T20:00:00Z', '2020-01-01T00:00:00Z')], out, store)
check(p.returncode == 0, 'generate.py exits 0', p.stderr[-300:])
html = open(out, encoding='utf-8').read()
badge = re.search(r'更新于[^<]*', html)
got = badge.group(0) if badge else ''
check('2020-01-01 08:00' in got,
      'badge = newest fetched_at, converted to Beijing (UTC+8)', got)
check(got.replace('更新于', '').strip() == '2020-01-01 08:00',
      'badge carries nothing but that timestamp', got)

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

print()
bad = results.count(False)
print(f'{len(results) - bad}/{len(results)} checks passed' + ('' if not bad else f' -- {bad} FAILURE(S)'))
sys.exit(1 if bad else 0)
