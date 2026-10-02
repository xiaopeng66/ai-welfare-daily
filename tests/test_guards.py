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
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FETCH_PY = os.environ.get('FETCH_PY') or (
    sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'fetch.py')
)

spec = importlib.util.spec_from_file_location('fetcher', FETCH_PY)
assert spec and spec.loader, f'cannot load {FETCH_PY}'
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _no_network(*a, **k):
    """Any real request during these tests is a bug in the test, not flakiness."""
    raise AssertionError(
        'a test tried to reach the network - inject the source function instead')


setattr(mod, 'fetch', _no_network)


def source_names():
    """Every source function, discovered by name so a newly added source cannot
    accidentally run for real just because nobody remembered to list it."""
    return sorted(n for n, v in vars(mod).items() if n.startswith('fetch_') and callable(v))


results = []


def check(ok, msg, extra=None):
    results.append(bool(ok))
    detail = '' if extra is None else f' :: {extra}'
    print(('ok    ' if ok else 'FAIL  ') + msg + (detail if not ok else ''))


def topic(tid, url, title, created='2026-10-01T10:00:00Z', verified=True, source=''):
    return {'id': str(tid), 'title': title, 'url': url, 'created_at': created,
            'published_verified': verified, 'source': source, 'tags': [], 'score': 0}


def run(store_rows, sources, limit=200, raw_store=None, workdir=None):
    """Drive main() against a temp store with injected sources.

    workdir reuses one directory across calls, so the store persists between
    runs - the incremental tests need exactly that.
    """
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
    sys.argv = ['fetch.py', '-o', out, '--limit', str(limit)]
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
store = [topic(1001, 'https://linux.sb/topic/1001', '额度 A'),
         topic(1002, 'https://linux.sb/topic/1002', '额度 B')]
r = run(store, {'fetch_linuxsb': lambda *a, **k: [topic(2001, 'https://linux.sb/topic/2001', '额度 new')],
                'fetch_nodeloc': lambda *a, **k: (mod.FETCH_ERRORS.append('injected'), [])[1]})
check(r['code'] == 0, 'partial failure still exits 0 (data is committed first)', r['code'])
check({x['id'] for x in r['rows']} == {'1001', '1002', '2001'}, 'cached rows kept beside the new one',
      [x['id'] for x in r['rows']])
check(r['marker'], 'partial failure leaves the marker file for CI')
check(r['litter'] == [], 'atomic write leaves no .tmp-* behind', r['litter'])

print('\n== total blackout refuses to write ==')
store = [topic(i, f'https://linux.sb/topic/{i}', '额度 x') for i in range(1, 6)]
r = run(store, {})
check(r['code'] == 2, 'blackout exits 2', r['code'])
check(r['before'] == r['after'], 'store byte-identical after a blackout')

print('\n== a damaged store is never treated as empty ==')
r = run(None, {}, raw_store='{"broken\n{"also broken\n')
check(r['code'] == 3, 'all lines unparseable exits 3', r['code'])
check(r['before'] == r['after'], 'store byte-identical after refusing')
good = json.dumps(topic(1, 'https://linux.sb/topic/1', '额度 1'), ensure_ascii=False)
r = run(None, {'fetch_linuxsb': lambda *a, **k: [topic(2, 'https://linux.sb/topic/2', '额度 2')]},
        raw_store=good + '\n{"broken json\n')
check(r['code'] == 0 and len(r['rows']) == 2 and any('unparseable' in e for e in r['errors']),
      'partially damaged store: good rows kept, damage reported', r['errors'])

print('\n== cross-site id collision keeps BOTH posts ==')
lsb = topic(12345, 'https://linux.sb/topic/12345', '额度 lsb', source='linuxsb_p1')
nod = topic(12345, 'https://nodeloc.com/t/topic/12345', '额度 nodeloc', source='nodeloc_p1')
r = run([lsb], {'fetch_nodeloc': lambda *a, **k: [nod]})
check(len(r['rows']) == 2 and sorted(x['url'] for x in r['rows']) == sorted([lsb['url'], nod['url']]),
      'both rows survived the clash', [x['id'] for x in r['rows']])
check(len({mod.topic_key(x) for x in r['rows']}) == 2, 'the two rows use distinct keys')

print('\n== the cap drops the OLDEST rows ==')
store = [topic(1000 + i, f'https://linux.sb/topic/{1000+i}', f'额度 {i}',
               created=f'2026-09-{i % 28 + 1:02d}T10:00:00Z') for i in range(250)]
r = run(store, {'fetch_linuxsb': lambda *a, **k: [topic(9999, 'https://linux.sb/topic/9999', '额度 n')]},
        limit=200)
check(len(r['rows']) == 200 and '9999' in {x['id'] for x in r['rows']}, 'cap honoured, newest kept',
      len(r['rows']))

print('\n== incremental store: only changed rows are rewritten ==')
wd = tempfile.mkdtemp(prefix='guard-incr-')
srcs = {'fetch_linuxsb': lambda *a_, **k: [topic(9001, 'https://linux.sb/topic/9001', '额度 A')]}
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
rows[0]['title'] = '额度 PERTURBED'
with open(r3['path'], 'w', encoding='utf-8') as f:
    f.write(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in rows))
r4 = run(None, srcs, workdir=wd)
check('1 updated' in r4['log'], 'a perturbed row is detected and bumped',
      r4['log'].strip().splitlines()[-1:])
check(r4['rows'][0]['title'] == '额度 A', 'the perturbation was repaired', r4['rows'][0]['title'])
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
r = run([], {'fetch_linuxsb': lambda *a, **k: [topic(7000, 'https://linux.sb/topic/7000', '额度 s')]})
need = {'id', 'title', 'url', 'source', 'created_at', 'fetched_at',
        'tags', 'score', 'published_verified'}
check(need <= set(r['rows'][0]), 'schema complete', need - set(r['rows'][0]))
shutil.rmtree(r['dir'], ignore_errors=True)

print()
bad = results.count(False)
print(f'{len(results) - bad}/{len(results)} checks passed' + ('' if not bad else f' -- {bad} FAILURE(S)'))
sys.exit(1 if bad else 0)
