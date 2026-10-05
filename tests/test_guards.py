#!/usr/bin/env python3
"""Guard tests for fetch.py: inject each failure instead of breaking the network.

Why this exists: every guard below is "do not lose data" logic, and the way such
code fails is by NOT firing. A pipeline that silently drops a source, lets one
site's row overwrite another's, or writes a fake change looks perfectly healthy
from the outside - CI stays green and the page keeps rendering.

Usage:
    python3 tests/test_guards.py [path/to/fetch.py]

Everything here is hermetic: sources are injected, so nothing touches the
network and the real store is never opened.
"""
import contextlib
import importlib.util
import urllib.error
import io
import json
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FETCH_PY = os.environ.get('FETCH_PY') or (
    sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'fetch.py')
)

spec = importlib.util.spec_from_file_location('fetcher', FETCH_PY)
assert spec and spec.loader, f'cannot load {FETCH_PY}'
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

STORE_PATH = os.path.join(ROOT, 'data', 'topics.jsonl')


def _store_titles():
    """Titles in the real store — only used to audit coverage, never mutated."""
    try:
        with open(STORE_PATH, encoding='utf-8') as f:
            return [json.loads(line)['title'] for line in f if line.strip()]
    except OSError:
        return []


def _no_network(*a, **k):
    """Any real request during these tests is a bug in the test, not flakiness."""
    raise AssertionError(
        'a test tried to reach the network - inject the source function instead')


setattr(mod, 'fetch', _no_network)

# run() swaps mod.probe_deleted for an injected fake, so keep the real one to test.
_real_probe_deleted = mod.probe_deleted
# And run() rebinds EVERY fetch_* source to a stub, never restoring them, so any
# test that wants to exercise a real source function must hold its own reference.
# (Discovered the hard way: a linux.do retry test appended at the END of this file
# called mod.fetch_linuxdo_welfare and got the stub -- 0 requests, and it looked
# like the retry was broken when it was working.)
_real_fetch_linuxdo_welfare = mod.fetch_linuxdo_welfare


def source_names():
    """Every source function, discovered by name so a newly added source cannot
    accidentally run for real just because nobody remembered to list it."""
    return sorted(n for n, v in vars(mod).items() if n.startswith('fetch_') and callable(v))


results = []


def check(ok, msg, extra=None):
    results.append(bool(ok))
    detail = '' if extra is None else f' :: {extra}'
    print(('ok    ' if ok else 'FAIL  ') + msg + (detail if not ok else ''))


def topic(tid, url, title, created='2026-10-01T10:00:00Z', verified=True, source='',
          fetched=None):
    t = {'id': str(tid), 'title': title, 'url': url, 'created_at': created,
         'published_verified': verified, 'source': source, 'tags': [], 'score': 0}
    if fetched:
        t['fetched_at'] = fetched
    return t


def run(store_rows, sources, limit=200, raw_store=None, workdir=None,
        probe=0, probe_fn=None):
    """Drive main() against a temp store with injected sources.

    workdir reuses one directory across calls, so the store persists between
    runs - the incremental tests need exactly that.

    probe defaults to 0 (删除探测关闭): mod.fetch is stubbed to raise, so a stray
    real request must fail loudly instead of being swallowed by probe_deleted's
    catch-all. Tests that exercise the probe inject probe_fn.
    """
    setattr(mod, 'probe_deleted',
            probe_fn if probe_fn is not None else (lambda url, timeout=8: False))
    d = workdir or tempfile.mkdtemp(prefix='guard-test-')
    out = os.path.join(d, 'topics.jsonl')
    if raw_store is not None:
        with open(out, 'w', encoding='utf-8') as f:
            f.write(raw_store)
    elif store_rows is not None:
        with open(out, 'w', encoding='utf-8') as f:
            f.write(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in store_rows))
    before = open(out, 'rb').read() if os.path.exists(out) else None
    mtime_before = os.path.getmtime(out) if os.path.exists(out) else None

    for name in source_names():
        setattr(mod, name, sources.get(name, lambda *a, **k: []))
    mod.FETCH_ERRORS.clear()
    old_argv = sys.argv
    sys.argv = ['fetch.py', '-o', out, '--limit', str(limit), '--probe-missing', str(probe)]
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
            mod.main()
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    finally:
        sys.argv = old_argv

    rows, junk = [], 0
    if os.path.exists(out):
        with open(out, encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:        # deliberately-corrupt fixtures
                    junk += 1
    res = {'code': code, 'rows': rows, 'junk': junk, 'errors': list(mod.FETCH_ERRORS),
           'log': buf.getvalue(), 'marker': os.path.exists(os.path.join(d, '.fetch_errors')),
           'litter': [f for f in os.listdir(d) if f.startswith('.tmp-')],
           'before': before, 'after': open(out, 'rb').read() if os.path.exists(out) else None,
           'mtime_before': mtime_before,
           'mtime_after': os.path.getmtime(out) if os.path.exists(out) else None,
           'dir': d, 'path': out}
    if workdir is None:
        shutil.rmtree(d, ignore_errors=True)
    return res


print('== sources are injected, not fetched ==')
names = source_names()
check(len(names) >= 5, f'every source function is overridden by the harness: {", ".join(names)}',
      names)

print('\n== partial failure keeps cached rows, and is reported ==')
store = [topic(1001, 'https://linux.sb/topic/1001', '公益站 A'),
         topic(1002, 'https://linux.sb/topic/1002', '公益站 B')]
r = run(store, {'fetch_linuxsb': lambda *a, **k: [topic(2001, 'https://linux.sb/topic/2001', '公益站 new')],
                'fetch_nodeloc': lambda *a, **k: (mod.FETCH_ERRORS.append('injected'), [])[1]})
check(r['code'] == 0, 'partial failure still exits 0 (data is committed first)', r['code'])
check({x['id'] for x in r['rows']} == {'1001', '1002', '2001'}, 'cached rows kept beside the new one',
      [x['id'] for x in r['rows']])
check(r['marker'], 'partial failure leaves the marker file for CI')
check(r['litter'] == [], 'atomic write leaves no .tmp-* behind', r['litter'])

print('\n== total blackout refuses to write ==')
store = [topic(i, f'https://linux.sb/topic/{i}', '公益站 x') for i in range(1, 6)]
r = run(store, {})
check(r['code'] == 2, 'blackout exits 2', r['code'])
check(r['before'] == r['after'], 'store byte-identical after a blackout')

print('\n== a damaged store is never treated as empty ==')
r = run(None, {}, raw_store='{"broken\n{"also broken\n')
check(r['code'] == 3, 'all lines unparseable exits 3', r['code'])
check(r['before'] == r['after'], 'store byte-identical after refusing')
good = json.dumps(topic(1, 'https://linux.sb/topic/1', '公益站 1'), ensure_ascii=False)
r = run(None, {'fetch_linuxsb': lambda *a, **k: [topic(2, 'https://linux.sb/topic/2', '公益站 2')]},
        raw_store=good + '\n{"broken json\n')
check(r['code'] == 0 and len(r['rows']) == 2 and any('unparseable' in e for e in r['errors']),
      'partially damaged store: good rows kept, damage reported', r['errors'])

print('\n== cross-site id collision keeps BOTH posts ==')
lsb = topic(12345, 'https://linux.sb/topic/12345', '公益站 lsb', source='linuxsb_p1')
nod = topic(12345, 'https://nodeloc.com/t/topic/12345', '公益站 nodeloc', source='nodeloc_p1')
r = run([lsb], {'fetch_nodeloc': lambda *a, **k: [nod]})
check(len(r['rows']) == 2 and sorted(x['url'] for x in r['rows']) == sorted([lsb['url'], nod['url']]),
      'both rows survived the clash', [x['id'] for x in r['rows']])
check(len({mod.topic_key(x) for x in r['rows']}) == 2, 'the two rows use distinct keys')

print('\n== the cap drops the OLDEST rows ==')
store = [topic(1000 + i, f'https://linux.sb/topic/{1000+i}', f'公益站 {i}',
               created=f'2026-09-{i % 28 + 1:02d}T10:00:00Z') for i in range(250)]
r = run(store, {'fetch_linuxsb': lambda *a, **k: [topic(9999, 'https://linux.sb/topic/9999', '公益站 n')]},
        limit=200)
check(len(r['rows']) == 200 and '9999' in {x['id'] for x in r['rows']}, 'cap honoured, newest kept',
      len(r['rows']))

print('\n== incremental store: only changed rows are rewritten ==')
wd = tempfile.mkdtemp(prefix='guard-incr-')
srcs = {'fetch_linuxsb': lambda *a_, **k: [topic(9001, 'https://linux.sb/topic/9001', '公益站 A')]}
r1 = run([], srcs, workdir=wd)
check('1 updated' in r1['log'], 'run 1: the new post is written', r1['log'].strip().splitlines()[-1:])
# "New" is a 24h window computed in the browser, so a post does not change state
# on the run after it appears: one new post now costs exactly one commit.
r2 = run(None, srcs, workdir=wd)
check('0 updated' in r2['log'], 'run 2: a post that has not changed is not rewritten',
      r2['log'].strip().splitlines()[-1:])
r3 = run(None, srcs, workdir=wd)
check('0 updated' in r3['log'], 'run 3: identical input is a no-op', r3['log'].strip().splitlines()[-1:])
check(r3['before'] == r3['after'], 'run 3: store bytes unchanged')
check(r3['mtime_after'] == r3['mtime_before'], 'run 3: the file is not even touched')
# Positive control: "0 updated" is also what a broken comparison prints, so prove
# the detector reacts to a real change before trusting the no-op run above.
rows = [json.loads(line) for line in open(r3['path'], encoding='utf-8') if line.strip()]
rows[0]['title'] = '公益站 PERTURBED'
with open(r3['path'], 'w', encoding='utf-8') as f:
    f.write(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in rows))
r4 = run(None, srcs, workdir=wd)
check('1 updated' in r4['log'], 'a perturbed row is detected and bumped',
      r4['log'].strip().splitlines()[-1:])
check(r4['rows'][0]['title'] == '公益站 A', 'the perturbation was repaired', r4['rows'][0]['title'])
r5 = run(None, srcs, workdir=wd)
check(r5['before'] == r5['after'], 'settles back to a byte-identical no-op')
shutil.rmtree(wd, ignore_errors=True)

print('\n== write_if_changed unit ==')
d = tempfile.mkdtemp(prefix='guard-wic-')
p = os.path.join(d, 'x.txt')
check(mod._write_if_changed(p, 'abc') is True, 'first write reports a change')
m1 = os.path.getmtime(p)
check(mod._write_if_changed(p, 'abc') is False, 'identical content reports no change')
check(os.path.getmtime(p) == m1, 'identical content does not touch the file')
check(mod._write_if_changed(p, 'abd') is True and open(p, encoding='utf-8').read() == 'abd',
      'different content is written')
shutil.rmtree(d, ignore_errors=True)

print('\n== every written row carries the full schema ==')
r = run([], {'fetch_linuxsb': lambda *a, **k: [topic(7000, 'https://linux.sb/topic/7000', '公益站 s')]})
need = {'id', 'title', 'url', 'source', 'created_at', 'fetched_at',
        'tags', 'score', 'published_verified'}
check(need <= set(r['rows'][0]), 'schema complete', need - set(r['rows'][0]))
shutil.rmtree(r['dir'], ignore_errors=True)

print('\n== cross-site id collision: same numeric id, two different sites ==')
# Regression: the store was keyed by the bare id, so these two rows loaded as ONE
# (last line wins) and, on a run where only one site returned data, the other
# site's row disappeared from the store. The key must carry the host, and it must
# be recomputable from the stored row so that a second run is a no-op.
LSB_URL = 'https://linux.sb/topic/12345'
NODE_URL = 'https://nodeloc.com/t/topic/12345'
stored = [topic(12345, LSB_URL, '鸡蛋 大放送', source='linuxsb_福利放送'),
          topic(12345, NODE_URL, '公益站 体验金', source='nodeloc')]
both = {
    'fetch_linuxsb': lambda *a, **k: [topic(12345, LSB_URL, '鸡蛋 大放送',
                                            source='linuxsb_福利放送')],
    'fetch_nodeloc': lambda *a, **k: [topic(12345, NODE_URL, '公益站 体验金',
                                            source='nodeloc')],
}
wd = tempfile.mkdtemp(prefix='guard-key-')
r1 = run(stored, both, workdir=wd)
check(len(r1['rows']) == 2, 'both sites keep their own row',
      [r['url'] for r in r1['rows']])
check('loaded 2 existing' in r1['log'], 'the store loads them as two rows',
      [line for line in r1['log'].splitlines() if 'loaded' in line])
r2 = run(None, both, workdir=wd)
check(len(r2['rows']) == 2, 'still two rows on the next run', len(r2['rows']))
check('0 updated' in r2['log'], 'and the next run reports no phantom change',
      r2['log'].strip().splitlines()[-1:])
check(r2['before'] == r2['after'], 'next run: store bytes unchanged')
r3 = run(None, {'fetch_nodeloc': both['fetch_nodeloc']}, workdir=wd)
check(len(r3['rows']) == 2, 'a run where linux.sb returned nothing keeps its row',
      [r['url'] for r in r3['rows']])
shutil.rmtree(wd, ignore_errors=True)

print('\n== the age cap must not evict a dateless row before an ancient one ==')
dateless = topic(9999, 'https://linux.do/t/9999', '公益站 无发布时间', created=None,
                 source='linuxdo_welfare', fetched='2026-10-02T06:00:00Z')
ancient = topic(8888, 'https://linux.sb/topic/8888', '鸡蛋 上古帖',
                created='2020-01-01T00:00:00Z', source='linuxsb_福利放送',
                fetched='2026-10-02T06:00:00Z')
r = run([dateless, ancient], {'fetch_linuxsb': lambda *a, **k: [ancient]}, limit=1)
check(len(r['rows']) == 1, 'the cap holds', len(r['rows']))
check(r['rows'] and r['rows'][0]['id'] == '9999',
      'the 2020 row goes first; a dateless row ages by its fetch time',
      [x['id'] for x in r['rows']])

print('\n== a source that stops emitting created_at must not erase a known one ==')
NURL = 'https://nodeloc.com/t/topic/7777'
dated = topic(7777, NURL, '公益站 体验金 送额度', created='2026-09-01T00:00:00Z',
              source='nodeloc')
# As stored, the row already carries the tags/score the fetcher computes (taken
# from the fetcher itself, so a keyword change cannot make this test lie), which
# leaves the missing publish time as the only difference.
_scored = mod.score_topic({'title': '公益站 体验金 送额度'})
dated.update(tags=_scored['tags'], score=_scored['score'])
undated = topic(7777, NURL, '公益站 体验金 送额度', created=None, source='nodeloc')
r = run([dated], {'fetch_nodeloc': lambda *a, **k: [undated]})
check(r['rows'] and r['rows'][0]['created_at'] == '2026-09-01T00:00:00Z',
      'the stored publish time survives',
      r['rows'][0]['created_at'] if r['rows'] else 'no row')
check('0 updated' in r['log'], 'and the missing field is not a change either',
      r['log'].strip().splitlines()[-1:])

print('\n== ended posts are removed on the next update ==')
# 用户 2026-10-04 决定：已结束就剔除，结束/删除的帖子下次更新时删掉。
ended = topic(4001, 'https://linux.sb/topic/4001', '【10-1】欢庆国庆，爽蹬$1000刀(已完)',
              source='linuxsb_福利放送')
alive = topic(4002, 'https://linux.sb/topic/4002', '公益站 B 免费领额度', source='linuxsb_福利放送')
r = run([ended, alive],
        {'fetch_linuxsb': lambda *a, **k: [topic(4003, 'https://linux.sb/topic/4003',
                                                '公益站 new 送额度', source='linuxsb_福利放送')]})
check({x['id'] for x in r['rows']} == {'4002', '4003'},
      'a stored post marked (已完) is dropped, its live neighbour is not',
      [x['id'] for x in r['rows']])
check('purge' in r['log'], 'the ended-post removal is logged',
      [line for line in r['log'].splitlines() if 'purge' in line])

print('\n== a post the source deleted is removed ==')
gone = topic(6001, 'https://linux.sb/topic/6001', '公益站 C 免费额度', source='linuxsb_福利放送')
kept = topic(6002, 'https://linux.sb/topic/6002', '公益站 D 免费额度', source='linuxsb_福利放送')
r = run([gone, kept],
        {'fetch_linuxsb': lambda *a, **k: [topic(6003, 'https://linux.sb/topic/6003',
                                                '公益站 E 送额度', source='linuxsb_福利放送')]},
        probe=15, probe_fn=lambda url, timeout=8: url.endswith('/6001'))
check({x['id'] for x in r['rows']} == {'6002', '6003'},
      'only the off-list row whose URL answers 404 is removed',
      [x['id'] for x in r['rows']])
check('confirmed deleted' in r['log'], 'the probe result is logged',
      [line for line in r['log'].splitlines() if 'probe' in line])

print('\n== a source that failed this run never loses rows to the probe ==')
# 源站这一轮没看成时，「不在列表里」只说明我们没看到，不说明帖子没了。
# 这里让探针一律撒谎说「已删除」，linux.sb 的行也必须活下来。
r = run([topic(7001, 'https://linux.sb/topic/7001', '公益站 F 免费额度', source='linuxsb_福利放送')],
        {'fetch_nodeloc': lambda *a, **k: [topic(7002, 'https://nodeloc.com/t/topic/7002',
                                                '公益站 G 送额度', source='nodeloc')]},
        probe=15, probe_fn=lambda url, timeout=8: True)
check({x['id'] for x in r['rows']} == {'7001', '7002'},
      'a source with no data this run keeps its rows even against a lying probe',
      [x['id'] for x in r['rows']])
check('checked 0 of' in r['log'], 'and the probe never even looked at that source',
      [line for line in r['log'].splitlines() if 'probe' in line])

print('\n== probe_deleted only believes an explicit 404/410 ==')


def _http(code):
    def f(url, timeout=None, **k):
        raise urllib.error.HTTPError(url, code, 'injected', {}, None)
    return f


mod.probe_deleted = _real_probe_deleted     # run() leaves an injected fake behind
for code, expected in ((404, True), (410, True), (403, False), (429, False),
                       (500, False), (503, False)):
    mod.fetch = _http(code)
    check(mod.probe_deleted('https://linux.sb/topic/1') is expected,
          f'HTTP {code} -> deleted={expected} '
          f'(a WAF block or an outage must never delete a row)')


def _timeout(url, timeout=None, **k):
    raise TimeoutError('injected')


mod.fetch = _timeout
check(mod.probe_deleted('https://linux.sb/topic/1') is False, 'a timeout is not a deletion')
mod.fetch = _no_network      # 恢复守卫：之后任何真请求照旧炸出来
check(mod.probe_deleted('') is False, 'an empty url is never a deletion')

print('\n== a stored row that no longer passes the gate is dropped ==')
# 闸门只判新抓到的行会漏掉一整类：用旧规则放进来的噪音没有任何一轮会重判它，
# 于是会一直留到被 cap 淘汰（实测 store 里积了 28 条，正是用户抱怨的那批）。
noise = topic(5001, 'https://linux.do/t/topic/5001', '发点积分，各位国庆节快乐呀',
              source='linuxdo_welfare')
fine = topic(5002, 'https://linux.do/t/topic/5002', '公益站 免费额度', source='linuxdo_welfare')
r = run([noise, fine],
        {'fetch_linuxsb': lambda *a, **k: [topic(5003, 'https://linux.sb/topic/5003',
                                                '公益站 new', source='linuxsb_福利放送')]})
check({x['id'] for x in r['rows']} == {'5002', '5003'},
      'a stored row the gate now rejects is removed, its neighbour is not',
      [x['id'] for x in r['rows']])
check('no longer pass the gate' in r['log'], 'and the gate purge is logged',
      [line for line in r['log'].splitlines() if 'purge' in line])

print('\n== a gate regression must not wipe the store ==')
# 30% 保险（外加 20 行下限）：一次删掉大半库说明闸门写错了，而不是噪音多。
big = [topic(6000 + i, f'https://linux.do/t/topic/{6000 + i}',
             '公益站 ok' if i < 15 else '发点积分，各位国庆节快乐呀', source='linuxdo_welfare')
       for i in range(25)]
r = run(big, {'fetch_linuxsb': lambda *a, **k: [topic(7000, 'https://linux.sb/topic/7000',
                                                     '公益站 new', source='linuxsb_福利放送')]})
check(len(r['rows']) == 26, '40% failing the gate leaves the store untouched', len(r['rows']))
check('REFUSED' in r['log'], 'and the refusal is logged loudly',
      [line for line in r['log'].splitlines() if 'purge' in line])
check(any('gate purge refused' in e for e in r['errors']), 'and it is reported to CI',
      r['errors'])
# 反向：同样 25 行、只有 20% 不过闸门 → 该删的照删（保险不能把正常清理也挡住）。
mild = [topic(8000 + i, f'https://linux.do/t/topic/{8000 + i}',
              '公益站 ok' if i < 20 else '发点积分，各位国庆节快乐呀', source='linuxdo_welfare')
        for i in range(25)]
r = run(mild, {'fetch_linuxsb': lambda *a, **k: [topic(9000, 'https://linux.sb/topic/9000',
                                                     '公益站 new', source='linuxsb_福利放送')]})
check(len(r['rows']) == 21, '20% failing the gate is cleaned up normally', len(r['rows']))
check(not any('gate purge refused' in e for e in r['errors']), 'and no refusal is reported',
      r['errors'])

print('\n== tags are recomputed for stored rows too ==')
# 打标过去只在抓取时发生，于是旧行的标签被永久冻结：按当时词表「优惠渠道」只有 6 条，
# 而当前词表实际该有 32 条。分类改了不重算，用户看到的就是旧结果。
old_tagged = topic(5501, 'https://linux.do/t/topic/5501', '为开发者送免费的gpt额度 0.35倍率',
                   source='linuxdo_welfare')
old_tagged['tags'] = ['额度']       # 旧规则的标签，缺 优惠渠道
old_tagged['score'] = 1
r = run([old_tagged], {'fetch_linuxsb': lambda *a, **k: [
    topic(5502, 'https://linux.sb/topic/5502', '公益站 new', source='linuxsb_福利放送')]})
byid = {x['id']: x for x in r['rows']}
check('优惠渠道' in byid['5501']['tags'],
      'a stored row is re-tagged with the current map', byid['5501']['tags'])
check(byid['5501']['score'] == len(byid['5501']['tags']),
      'score follows the recomputed tags', byid['5501']['score'])
check('re-tagged' in r['log'], 'and the re-tag pass is logged',
      [line for line in r['log'].splitlines() if 'tags' in line])

print('\n== a category word must name the OBJECT, not a generic action ==')
# 「额度」曾靠裸「送」给 18 条不含额度/刀的帖打标；「兑换码」曾靠裸 `code` 命中
# `zcode`（zcode 的臭鸡蛋）和 `codex`（claude code 公益站）。
for title, bad_tag, why in [
        ('中午好 国模鸡蛋放送', '额度', 'a bare 送 does not tag 额度'),
        ('zcode的臭鸡蛋快领吧', '兑换码', 'a bare code must not match zcode'),
        ('免费claude code公益站，注册就送100刀', '兑换码', 'nor claude code'),
        ('【xxzl公益API】正式开业｜注册即送 $150', '公益站_free', 'non-issue guard')]:
    if why == 'non-issue guard':
        continue
    check(bad_tag not in mod.score_topic({'title': title})['tags'], why,
          mod.score_topic({'title': title})['tags'])
# 正向：这些确实该打上对应标签。
check('鸡蛋' in mod.score_topic({'title': 'zcode的臭鸡蛋快领吧'})['tags'],
      'but the egg post is still tagged 鸡蛋')
check('额度' in mod.score_topic({'title': '注册就送100刀额度'})['tags'],
      'an explicit 额度 is still tagged')
check('兑换码' in mod.score_topic({'title': '发一些积分码和邀请码'})['tags'],
      'a real 邀请码 is still tagged 兑换码')

print('\n== every stored row should carry at least one category ==')
# 无标签帖曾积到 64/267（24%）—— 落在页面上就是「分类筛选怎么点都筛不出它」。
# 补出「免费放粮」后收到 18 条。这里钉住的是不反弹（不是要求归零：真有可能出现
# 讲新东西的帖，硬凑一个分类比空着更糟）。
untagged = [t for t in _store_titles() if not mod.score_topic({'title': t})['tags']]
check(len(untagged) <= len(_store_titles()) * 0.10,
      'at most ~10% of the store is untagged', f'{len(untagged)}/{len(_store_titles())}')
for t in untagged:
    print('   untagged:', t[:76])

print('\n== linux.do 的 429 要重试一次，而不是丢掉整页 ==')
# 抓取节奏提到每小时后实测：17 轮里 4 轮 page2 拿到 429、1 轮 page1 拿到 429，
# 而旧代码对 429 直接 continue 到下一页 —— 整页 30 条静默丢失，且运行仍然「成功」，
# 没有任何红灯。这里用桩件把三种路径钉住。
class _FakePage:
    def __init__(self, status, body):
        self.status = status
        self.body = body


_ld_topics = [{'id': i, 'title': f'注册送100刀公益站{i}', 'created_at': '2026-10-05T01:00:00Z'}
              for i in range(5)]
_ld_good = json.dumps({'topic_list': {'topics': _ld_topics}}) + ' ' * 1500  # 过 1000 字节门槛
_orig_scrapling = sys.modules.get('scrapling')
_orig_sleep = mod.time.sleep

_ld_calls, _ld_slept = [], []
mod.time.sleep = lambda s: _ld_slept.append(s)

# ① 首次 429 → 退避一次后成功：两页都要拿到
def _f_429_then_ok(url, headless=True, timeout=0):
    _ld_calls.append(url)
    return _FakePage(429, 'x' * 2000) if len(_ld_calls) == 1 else _FakePage(200, _ld_good)


sys.modules['scrapling'] = types.SimpleNamespace(
    StealthyFetcher=types.SimpleNamespace(fetch=_f_429_then_ok))
mod.FETCH_ERRORS.clear()
_ld_calls.clear()
_ld_slept.clear()
_rows = _real_fetch_linuxdo_welfare()
check(len(_rows) == 10, 'a 429 on the first attempt is retried and the page is recovered',
      f'{len(_rows)} rows')
check(_ld_slept == [30], 'the retry waits before re-requesting (not a hot loop)', _ld_slept)

# ② 一直 429 → 放弃并停止，不再去打第二页（也不该被记成「status=200 失败」）
def _f_always_429(url, headless=True, timeout=0):
    _ld_calls.append(url)
    return _FakePage(429, 'x' * 2000)


sys.modules['scrapling'] = types.SimpleNamespace(
    StealthyFetcher=types.SimpleNamespace(fetch=_f_always_429))
mod.FETCH_ERRORS.clear()
_ld_calls.clear()
_ld_slept.clear()
_rows = _real_fetch_linuxdo_welfare()
check(_rows == [], 'a persistent 429 yields no rows rather than garbage', len(_rows))
check(len(_ld_calls) == 2, 'after a persistent 429 page 1 gives up (2 attempts, no page 2)',
      len(_ld_calls))

# ③ 全 200 → 恰好两页两次请求、零睡眠（重试不能拖慢正常路径）
def _f_ok(url, headless=True, timeout=0):
    _ld_calls.append(url)
    return _FakePage(200, _ld_good)


sys.modules['scrapling'] = types.SimpleNamespace(
    StealthyFetcher=types.SimpleNamespace(fetch=_f_ok))
mod.FETCH_ERRORS.clear()
_ld_calls.clear()
_ld_slept.clear()
_rows = _real_fetch_linuxdo_welfare()
check(len(_rows) == 10 and len(_ld_calls) == 2 and not _ld_slept,
      'the healthy path fetches each page exactly once and never sleeps',
      f'{len(_rows)} rows, {len(_ld_calls)} requests, slept={_ld_slept}')

mod.time.sleep = _orig_sleep
if _orig_scrapling is not None:
    sys.modules['scrapling'] = _orig_scrapling
else:
    sys.modules.pop('scrapling', None)

print()
bad = results.count(False)
print(f'{len(results) - bad}/{len(results)} checks passed' + ('' if not bad else f' -- {bad} FAILURE(S)'))
sys.exit(1 if bad else 0)
