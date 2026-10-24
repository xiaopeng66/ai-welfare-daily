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

# 自己的 stdio 也钉成 UTF-8：断言文案/失败详情里会出现非 GBK 字符（✕ 🔥 🉐 等），
# 一旦 stdout 被重定向而 PYTHONIOENCODING 没设（手工 `python tests/xxx.py > log`），
# print 会当场 UnicodeEncodeError —— 与内容正确性无关的崩溃，还可能专挑打印失败详情
# 时炸，把真正的失败盖掉。reconfigure 不可用（stdout 被换成 StringIO 等）就跳过。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


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


# 2026-10-17：subprocess 的编码两侧写死。子进程 stdio 由 PYTHONIOENCODING 决定
# （run-update.ps1 设成 utf-8），但 text=True 的解码走 locale（中文 Windows = cp936）。
# 不一致时读管道的线程抛 UnicodeDecodeError、p.stderr 变 None，套件崩在无关断言上
# —— 主站腿连续 7 轮红就是这样来的。显式 utf-8 + replace 后与 locale/PYTHONUTF8 无关。
_GEN_ENV = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
_GEN_RUN = dict(capture_output=True, text=True, encoding='utf-8',
                errors='replace', env=_GEN_ENV)


def run_gen(args):
    return subprocess.run(args, **_GEN_RUN)


def build(rows, out, store):
    with open(store, 'w', encoding='utf-8') as f:
        f.write(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    return run_gen([sys.executable, GEN_PY, '-i', store, '-o', out])


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

print('\n== placeholder tokens in a title never corrupt the payload ==')
# 2026-10-23 审计 P0-2：六次顺序 str.replace 会重扫已插入的帖子 JSON —— 标题里只要
# 出现 __SOURCE_LABELS_JSON__ 之类的字面量，JSON 就被改坏、cards 消失、整页白。
# 单遍 re.sub 之后，这些字面量必须原样留在标题里，载荷仍可 raw_decode。
_TOKENS = ['__CARDS_JSON__', '__SOURCE_LABELS_JSON__', '__CATEGORY_TONES_JSON__',
           '__CATEGORY_ORDER_JSON__', '__FEATURED_JSON__', '__FEATURED_BAKED_AT__']
_evil_title = '免费 ' + ' + '.join(_TOKENS) + ' 中转站'
p = build([row(9100, _evil_title, '2026-10-01T10:00:00Z', '2026-10-01T12:00:00Z')], out, store)
check(p.returncode == 0, 'generate.py exits 0 with placeholder tokens in the title', p.stderr[-300:])
html = open(out, encoding='utf-8').read()
try:
    cards, _payload = cards_of(html)
    ok_payload, why = True, ''
except Exception as exc:                       # noqa: BLE001 - 断言要看到具体异常
    cards, ok_payload, why = [], False, f'{type(exc).__name__}: {exc}'
check(ok_payload, 'cards payload still parses (raw_decode) with every token in the title', why)
check(bool(cards) and cards[0]['title'] == _evil_title,
      'the token text survives verbatim inside the title',
      cards[0]['title'] if cards else None)
check(html.count('const labels=') == 1 and '"linuxsb"' in html,
      'the real source-labels payload is still injected exactly once')

print('\n== rendering the same store twice is a no-op ==')
rows = [row(1, '额度 A', '2026-09-30T10:00:00Z', '2026-10-01T12:00:00Z'),
        row(2, '抽奖 </script><img src=x onerror=alert(1)>', '2026-10-01T10:00:00Z',
            '2026-10-01T12:00:00Z'),
        row(3, '体验金 C', None, '2026-10-01T12:00:00Z')]
PICK_BAKE = re.compile(rb'const PICK_BAKED_AT=(\d+);')


def flatten_bake(b):
    """抹平精选倒计时的基准时刻（PICK_BAKED_AT）。

    这是页面上唯一一个**有意**来自时钟的值（倒计时用"生成时刻 + 已开时长"当 now，
    不信访客的设备时间），两次渲染隔不到一秒也可能跨秒。2026-10-16 之前这里直接比
    整份字节，属于"偶尔红一次"的假失败；抹平它之后其余（含整份 FEATURED 数据）仍是
    逐字节比较，令牌本身另外验一次"确实是刚生成的时钟值"。
    """
    return PICK_BAKE.sub(b'const PICK_BAKED_AT=0;', b)


def bake_of(b):
    m = PICK_BAKE.search(b)
    return int(m.group(1)) if m else None


p1 = build(rows, out, store)
first = open(out, 'rb').read()
mtime = os.path.getmtime(out)
p2 = build(rows, out, store)
second = open(out, 'rb').read()
check(first == second, 'byte-identical output（整文件比对，P1-10）',
      f'{digest(first)} vs {digest(second)}')
b1, b2 = bake_of(first), bake_of(second)
check(b1 is not None and b1 == b2,
      '倒计时基准两次渲染完全相同（不再是墙上时钟）', f'{b1} vs {b2}')
_newest = max(
    int(__import__('datetime').datetime.fromisoformat(r['fetched_at'].replace('Z', '+00:00')).timestamp())
    for r in rows
)
check(b1 == _newest, 'PICK_BAKED_AT = store 最新 fetched_at（纯函数，P1-10）',
      f'{b1} vs {_newest}')
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
    run_gen([sys.executable, GEN_PY, '-i', STORE, '-o', tmp])
    a, b = open(tmp, 'rb').read(), open(PAGE, 'rb').read()
    # 抹平两类"非 store 输入"后再逐字节比较：
    #   ① PICK_BAKED_AT：倒计时基准，每次生成都不同；
    #   ② FEATURED：精选板依赖 data/cache/content 的正文缓存（gitignored，§5.5）。
    #      CI 检出仓库后没有缓存，load_cache 抛错 → 精选区置空，与本地（有缓存、
    #      新鲜重建）天然不同。它的内容正确性由本地管道提交前的同一道门禁
    #      （有缓存）与 test_content 覆盖；这里只验证注入点还在、结构合法。
    # 比对 cards payload（主内容）而非整页字节——整页字节受行尾（CRLF/LF）、
    # 编码、正文缓存等非 store 输入影响（CI ubuntu vs Windows 的渲染天然不同）。
    # cards payload 是从 store 确定性渲染的 JSON，不受这些影响。
    def cards_of(data):
        text = data.decode('utf-8')
        i = text.index('const cards=')
        start = text.index('[', i)
        depth = 0
        for j in range(start, len(text)):
            if text[j] == '[': depth += 1
            elif text[j] == ']':
                depth -= 1
                if depth == 0: return text[start:j+1]
        return None

    ca, cb = cards_of(a), cards_of(b)
    check(ca is not None and cb is not None, 'both pages have a cards payload')
    check(ca == cb, 'docs/index.html cards match the committed store',
          f'{digest(ca.encode())[:12]} != {digest(cb.encode())[:12]} - regenerate and commit the page')
    # 活动口径（2026-10-19）：窗口上限按「活动」算，页面折叠键必须与 fetch 完全同构。
    # 两边不一致时 hero 会超过 300（线上实测 302），而 fetch 的日志算 299。
    spec = importlib.util.spec_from_file_location('fetch_activity', os.path.join(ROOT, 'fetch.py'))
    fetch_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch_mod)
    activities = {fetch_mod.activity_key(r) for r in json.loads(ca)}
    check(len(activities) <= fetch_mod.DEFAULT_LIMIT,
          'the committed page folds to at most DEFAULT_LIMIT activities',
          f'{len(activities)} > {fetch_mod.DEFAULT_LIMIT}')
    # 精选板结构：注入点在、非空、每张卡都有合法 kind（内容对错由有缓存的本地门禁管）
    m = re.search(rb'const FEATURED=(\[[^\n]*\]);', b)
    check(m is not None, 'the featured payload is embedded in the committed page', None)
    if m:
        feat = json.loads(m.group(1).decode('utf-8'))
        check(isinstance(feat, list) and len(feat) > 0, 'the featured payload is a non-empty list', len(feat))
        bad = [c.get('f', {}).get('name') for c in feat if c.get('f', {}).get('kind') not in ('gift', 'lottery', 'free')]
        check(not bad, 'every featured card carries a valid kind', bad[:3])

    # 访问统计的打点必须还在（2026-10-24 加）。
    # 由头：打点代码只存在于根目录的 template.html，**改页面时删掉/改坏它，发布链完全无感** ——
    # 实测把整段打点删掉后七套件仍然全绿（页面照发、CI 照绿），只有"打点链探针"能在
    # 12 小时后发现。加这条断言，这类改动当场红，不必等半天。
    page_text = b.decode('utf-8')
    check('https://welfare-hit.1847531284.workers.dev/' in page_text,
          'the visit beacon endpoint is baked into the committed page', None)
    check('navigator.sendBeacon(HIT' in page_text,
          'the beacon actually calls sendBeacon', None)
    check(page_text.count('welfare-vid') >= 2,
          'the visitor id is both read and written (welfare-vid)',
          page_text.count('welfare-vid'))
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
_tpl = open(os.path.join(ROOT, 'template.html'), encoding='utf-8').read()
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
    # 2026-10-19：没有 key 时 campaign 也参与（与 fetch.activity_key 同构）
    ('offer 折叠分支', '(c.offer&&(c.offer.key||c.offer.campaign))'),
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
    # 2026-10-15 审计：折叠键不能含 NUL（HTML 属性解析换成 U+FFFD，点击查不到组）；
    # 筛选/搜索要在原始行上做（被折掉的成员才搜得到）；弹窗按当前渲染的那张卡取组员。
    ('折叠键无 NUL', "'|#|'"),
    ('筛选用原始行', 'function matches(c){'),
    ('弹窗读当前渲染卡', 'cardByKey'),
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
