#!/usr/bin/env node
/**
 * Verify the published page's own JavaScript (v6 template): the "近 24 小时"
 * split, the fallback for rows with no created_at, and the time the cards print.
 *
 * The v6 page groups by section key decided in the browser (not baked into the
 * data), so a Python test cannot see it: this runs the real <script> from
 * docs/index.html against a minimal DOM shim, with a payload whose timestamps
 * are relative to now.
 *
 * Usage: node tests/test_page_js.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const PAGE = path.join(ROOT, 'docs', 'index.html');

let failed = 0;
function check(ok, msg, extra) {
  if (!ok) failed += 1;
  console.log(`${ok ? 'ok    ' : 'FAIL  '}${msg}${!ok && extra !== undefined ? ` :: ${extra}` : ''}`);
}

// --- payload fixture: timestamps relative to "now", which is the whole point --
const HOUR = 3600 * 1000;
const iso = (ms) => new Date(ms).toISOString();
const t = Date.now();
function row(id, title, created, fetched) {
  return {
    id, title, url: `https://linux.sb/topic/${id}`, tags: ['额度'], score: 1,
    source: 'linuxsb_福利放送', created_at: created, fetched_at: fetched,
    published_verified: true,
  };
}
const cards = [
  row('1', '一小时前发的', iso(t - 1 * HOUR), iso(t - 1 * HOUR)),
  row('2', '二十三小时前发的', iso(t - 23 * HOUR), iso(t - 23 * HOUR)),
  row('3', '二十五小时前发的', iso(t - 25 * HOUR), iso(t - 25 * HOUR)),
  row('4', '十天前发的', iso(t - 240 * HOUR), iso(t - 240 * HOUR)),
  // created_at missing (the legacy HTML path): the card falls back to fetched_at,
  // so the window has to use the same fallback or these rows lose their section.
  row('5', '无发布时间但刚抓到', null, iso(t - 2 * HOUR)),
  row('6', '无发布时间且抓到很久了', null, iso(t - 30 * HOUR)),
];

// --- load the page and swap in that payload -----------------------------------
const html = fs.readFileSync(PAGE, 'utf8');
const start = html.indexOf('const cards=[');
if (start < 0) throw new Error('docs/index.html no longer embeds const cards=[');
let depth = 0, end = -1, inStr = false, inEscape = false;
for (let i = html.indexOf('[', start); i < html.length; i += 1) {
  const ch = html[i];
  if (inStr) {
    if (inEscape) inEscape = false;
    else if (ch === '\\') inEscape = true;
    else if (ch === '"') inStr = false;
    continue;
  }
  if (ch === '"') inStr = true;
  else if (ch === '[') depth += 1;
  else if (ch === ']') { depth -= 1; if (depth === 0) { end = i + 1; break; } }
}
if (end < 0) throw new Error('unbalanced payload brackets');
// Replace only the payload array, keeping the `const cards=` declaration.
const payload = html.slice(html.indexOf('[', start), end);
// v6 embeds TWO script blocks: a tiny theme bootstrap and the app. The app is
// the LAST one, and it is the only one that references `cards`.
const scriptBlocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const inlineJs = scriptBlocks[scriptBlocks.length - 1];
if (!inlineJs.includes('const cards=')) throw new Error('the last <script> is no longer the app script');
const script = inlineJs.replace(payload, JSON.stringify(cards));

// --- minimal DOM --------------------------------------------------------------
// v6 uses querySelector/append/replaceChildren/insertAdjacentHTML/hidden, so the
// shim is slightly richer than the old page's: elements are tracked by selector
// where the page looks them up by id, and sections are plain objects.
const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
function makeEl(tag = 'div') {
  const el = {
    tag, children: [], _html: '', _text: undefined, style: {}, dataset: {},
    className: '', hidden: false,
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {},
    setAttribute() {}, getAttribute: () => null,
    append(...nodes) { el.children.push(...nodes); },
    replaceChildren(...nodes) { el.children = nodes; },
    insertAdjacentHTML(_pos, markup) { el.children.push({ __html: markup }); },
    // scroll props: updateRail touches them on any rail element the moment the
    // script boots, so every shim element carries a safe zero set.
    scrollWidth: 0, clientWidth: 0, scrollLeft: 0, scrollBy() {},
    querySelector() { return (typeof getNullEl === 'function' ? getNullEl() : null); },
    querySelectorAll: () => [],
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return el.children.filter((c) => c.__html !== undefined).map((c) => c.__html).join('') || el._html; },
    set(v) { el._html = v; el.children = [{ __html: v }]; },
  });
  Object.defineProperty(el, 'textContent', {
    set(v) { el._text = v; },
    get() { return el._text ?? (el.children.length ? esc(el._html) : ''); },
  });
  return el;
}
const registry = new Map();
function byId(id) {
  if (!registry.has(id)) registry.set(id, makeEl('div'));
  return registry.get(id);
}
function nullEl() {
  const e = makeEl('div');
  e.scrollWidth = 0; e.clientWidth = 0; e.scrollLeft = 0;
  e.scrollBy = () => {}; e.querySelector = () => null;
  e.querySelectorAll = () => []; e.dataset = {}; e.textContent = '';
  return e;
}
function build(payloadScript) {
  registry.clear();
  const sharedNull = nullEl();
  globalThis.getNullEl = () => sharedNull;
  const doc = {
    getElementById: byId,
    querySelector: (sel) => (sel.startsWith('#') ? byId(sel.slice(1)) : sharedNull),
    querySelectorAll: () => [],
    createElement: (tag) => makeEl(tag),
    documentElement: { dataset: {} },
    hidden: false,
    addEventListener() {},
  };
  const api = new Function('document', 'localStorage', 'matchMedia', 'setInterval',
    'requestAnimationFrame', 'ResizeObserver', 'window',
    `${payloadScript};return {refresh,isRecent,formatTime,bjDay,getCards:()=>cards,sectionLabel,makeEntries,renderCard,state};`)(
    doc,
    { getItem: () => null },
    () => ({ matches: false }),
    () => {},
    (fn) => fn(),               // requestAnimationFrame: run synchronously
    class { observe() {} },     // ResizeObserver: no-op
    { addEventListener() {} },  // window
  );
  return { api, doc };
}

const { api, doc: document } = build(script);

console.log('== the 24-hour window ==');
check(api.isRecent({ created_at: iso(t - 1 * HOUR) }) === true, '1 hour old is recent');
check(api.isRecent({ created_at: iso(t - 23 * HOUR) }) === true, '23 hours old is recent');
check(api.isRecent({ created_at: iso(t - 25 * HOUR) }) === false, '25 hours old is not recent');
check(api.isRecent({ created_at: null, fetched_at: iso(t - 2 * HOUR) }) === true,
  'no created_at falls back to fetched_at');
check(api.isRecent({ created_at: null, fetched_at: null }) === false,
  'no timestamp at all is not recent');
check(api.isRecent({}) === false, 'a row with neither field is not recent');

console.log('\n== the rendered sections (refresh() drives v6) ==');
// default sort is 'latest' → sections keyed by Beijing day. Force the
// recent/earlier split the way the "匹配度" sort does, by flipping state.sort
// before refresh - same code path the page itself uses for the 24h grouping.
api.state.sort = 'score';
api.refresh();
const results = byId('results');
// section elements are created via document.createElement and appended; their
// innerHTML lives on the child objects in the shim.
const sectionsHtml = results.children.map((c) => c.innerHTML).join('\n');
const allHtml = results.innerHTML + sectionsHtml;
check(allHtml.includes('近 24 小时'), 'a section heading says 近 24 小时', allHtml.slice(0, 120));
const recentIds = api.state.entries.filter((x) => x.key === 'recent').map((x) => x.c.id).sort();
const earlierIds = api.state.entries.filter((x) => x.key === 'earlier').map((x) => x.c.id).sort();
check(JSON.stringify(recentIds) === JSON.stringify(['1', '2', '5']),
  'recent section = rows inside the 24h window (created_at OR fetched_at)', recentIds);
check(JSON.stringify(earlierIds) === JSON.stringify(['3', '4', '6']),
  'older rows land outside it', earlierIds);
check(api.sectionLabel('recent') === '近 24 小时' && api.sectionLabel('earlier') === '更早的信息',
  'section labels name the split');

console.log('\n== latest sort groups by Beijing day, not UTC ==');
api.state.sort = 'latest';
api.refresh();
const keys = [...new Set(api.state.entries.map((x) => x.key))];
const bjDayOfRow1 = api.bjDay({ created_at: iso(t - 1 * HOUR) });
check(keys[0] === bjDayOfRow1, 'group key is the Beijing calendar day', keys[0]);
// deterministic boundary case: 20:00 UTC must group under the NEXT Beijing day,
// never under its own UTC date (the bug this guards: 19/161 rows once misfiled).
const cross = { ...row('9', '跨日样本', '2026-10-01T20:00:00Z', '2026-10-01T20:00:00Z') };
check(api.bjDay(cross) === '2026-10-02' && !keys.includes('2026-10-01') || api.bjDay(cross) === '2026-10-02',
  'bjDay puts 20:00 UTC on the next Beijing day', api.bjDay(cross));

console.log('\n== an empty result set explains itself ==');
api.state.query = '绝不存在的关键词xyz';
api.refresh();
check(byId('results').innerHTML.includes('没有找到匹配的信息'),
  'the no-results state renders', byId('results').innerHTML.slice(0, 100));
check(byId('count').textContent.includes('0 /'), 'the counter reads 0', byId('count').textContent);

console.log('\n== a malformed row cannot blank the page or inject markup ==');
const nasty = [
  { ...row('801', '标签不是数组', iso(t - 60 * 60 * 1000), iso(t - 60 * 60 * 1000)), tags: '额度' },
  { ...row('802', '标题带标签', iso(t - 60 * 60 * 1000), iso(t - 60 * 60 * 1000)),
    title: '<script>alert(1)</script>', score: '"><img src=x onerror=alert(1)>' },
];
let threw = null;
let html3 = '';
try {
  const built3 = build(inlineJs.replace(payload, JSON.stringify(nasty)));
  built3.api.state.sort = 'score';
  built3.api.refresh();
  // escapeHTML is applied inside renderCard, so assert on the rendered markup:
  // every appended section's innerHTML, which is what a browser would parse.
  html3 = built3.api.state.entries.map((x) => built3.api.renderCard(x.c)).join('');
} catch (e) { threw = e.message; }
check(threw === null, 'refresh() survives a string tags field', threw);
check(html3.includes('标签不是数组') && html3.includes('&lt;script&gt;alert(1)'),
  'both rows still render (the second one escaped)', html3.slice(0, 160));
check(!/<img|onerror=/.test(html3), 'score cannot smuggle an event handler in',
  html3.match(/<img[^>]*>/)?.[0]);
check(html3.includes('&lt;script&gt;') && !html3.includes('<script>alert'),
  'a title with markup stays text');

console.log('\n== the card clock is Beijing, like the date groups ==');
// 20:00 UTC is 04:00 the next day in Beijing. A browser-local formatting would
// print 20:00 on a UTC runner and disagree with the grouping above it.
const beijing = api.formatTime('2026-10-01T20:00:00Z');
check(beijing.includes('10-02') && beijing.includes('04:00'),
  'a UTC timestamp renders as its Beijing wall time', beijing);
const group = api.bjDay({ created_at: '2026-10-01T20:00:00Z' });
check(group === '2026-10-02', 'and the day grouping agrees', group);

console.log();
console.log(failed ? `${failed} FAILURE(S)` : 'all page-JS checks passed');
process.exit(failed ? 1 : 0);
