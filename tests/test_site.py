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
check(len(cards) == 3, 'every stored row is embedded in the payload (fold is render-time)', len(cards))
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




print('\n== 右下角悬浮按钮（返回顶部 / 刷新）==')
# 与 ai-welfare-promo-health.html 用同一套实现。模板是唯一真源，产物由 generate.py
# 渲染，所以两边都要盯：只查产物的话，模板丢了 fab 要等下次重新生成才暴露；
# 只查模板的话，占位符替换出错也会漏掉。
_tpl = open(os.path.join(ROOT, 'docs', 'template.html'), encoding='utf-8').read()
_FAB = [
    ('返回顶部按钮存在', 'id="fabTop"'),
    ('刷新按钮存在', 'id="fabReload"'),
    ('两个按钮在同一个容器里', 'class="fab" role="group"'),
    ('容器竖排', 'flex-direction:column'),
    ('固定在视口右下', 'position:fixed'),
    ('阴影走主题变量（深色模式自动跟随）', 'var(--fab-shadow)'),
    ('返回顶部默认是收起的', '.fab-btn.top{opacity:0'),
    ('刷新点击时图标转圈', '@keyframes fabspin'),
    ('初始化等 DOM 解析完（同步取元素会静默失效）', 'DOMContentLoaded'),
    ('窄屏适配', 'max-width:600px'),
]
for _label, _needle in _FAB:
    check(_needle in _tpl, 'template: ' + _label, _needle)
    check(_needle in _site_html, 'rendered: ' + _label, _needle)
# 浅色 + 深色两套都要有 --fab-shadow，漏一个暗色下按钮就没阴影
check(_tpl.count('--fab-shadow:') == 2,
      '两套主题都定义了 --fab-shadow（浅色 + 深色）', _tpl.count('--fab-shadow'))
# 返回顶部与刷新必须在同一行 HTML 里相邻（竖排顺序：上返回顶部、下刷新）
check(_tpl.index('id="fabTop"') < _tpl.index('id="fabReload"'),
      '竖排顺序：返回顶部在上、刷新在下', None)

print('\n== 同来源同标题折叠（渲染层）==')
# 折叠发生在页面 JS（store 不动、payload 仍是全量，见 payload integrity）。模板是
# 唯一真源，产物由 generate.py 渲染 —— 两边都必须带上折叠函数、×N 角标与说明文案，
# 否则「模板改了但页面没重生成」（只查产物会漏）或「占位符替换出错」（只查模板会漏）。
_FOLD = [
    ('折叠函数存在', 'function foldCards('),
    ('折叠结果喂给渲染', 'const view=foldCards(cards)'),
    ('角标样式存在', '.dups{'),
    ('角标标题文案', '已折叠显示最新一条'),
    # 2026-10-15 正文层：同一份 offer（offer.key）也参与折叠
    ('offer 折叠分支', '(c.offer&&c.offer.key)'),
    ('offer 角标文案', '同一份福利共'),
    ('提示文案含正文判定', '正文判定为同一份福利'),
    # 2026-10-15 用户要求：角标更显眼（彩色胶囊）且点开可看该组清单
    ('彩色胶囊样式', '.dups.hot{'),
    ('弹窗遮罩与盒子样式', '.modal-box{'),
    ('弹窗容器标记', 'id="foldModal"'),
    ('弹窗内容构建器', 'function foldModalHTML('),
    ('打开/关闭弹窗', 'function closeFoldModal('),
    ('角标是可点击按钮', 'data-fold="'),
    ('按钮打开弹窗', 'b.dataset.fold!==undefined'),
    ('遮罩/✕ 关闭', 'data-fold-close'),
    # 遮罩是 div、✕ 是 button：点击代理必须先认 [data-fold-close]，只认 button 会吞掉点遮罩
    ('点遮罩关闭的接线', "closest('[data-fold-close]')"),
]
for _label, _needle in _FOLD:
    check(_needle in _tpl, 'template: ' + _label, _needle)
    check(_needle in _site_html, 'rendered: ' + _label, _needle)


# 退出码：CI（.github/workflows/tests.yml 的 run:）和本地管道（run-update.ps1 step 2.5）
# 都靠它判断是否放行。2026-10-15 之前这里只往 results 里塞、从不检查，于是 FAIL
# 只打印不阻断、套件永远 exit 0 —— RULES.md 说的"不一致就红"在实现上从未生效，
# run-update.ps1 里那句 "refusing to commit" 也永远不会触发。补上。
_failed = results.count(False)
print("\n%d/%d checks passed%s" % (
    len(results) - _failed, len(results),
    ("，%d FAILED" % _failed) if _failed else ""))
sys.exit(1 if _failed else 0)
