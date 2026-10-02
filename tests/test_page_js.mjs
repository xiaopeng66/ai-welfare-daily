#!/usr/bin/env node
/**
 * Verify the published page's own JavaScript: the "近 24 小时" split, the
 * fallback for rows with no created_at, and the time the cards print.
 *
 * The split is decided in the browser (not baked into the data), so a Python
 * test cannot see it: this runs the real <script> from docs/index.html against a
 * minimal DOM shim, with a payload whose timestamps are relative to now.
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
const inlineJs = html.slice(html.indexOf('<script>') + '<script>'.length, html.indexOf('</script>'));
const script = inlineJs.replace(payload, JSON.stringify(cards));

// --- minimal DOM --------------------------------------------------------------
const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
function makeEl(id) {
  const el = {
    id, innerHTML: '', textContent: '', style: {}, dataset: {},
    classList: { add() {}, remove() {} },
    addEventListener() {},
    querySelectorAll: () => [],
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._text !== undefined ? esc(el._text) : el._html ?? ''; },
    set(v) { el._html = v; el._text = undefined; },
  });
  Object.defineProperty(el, 'textContent', {
    set(v) { el._text = v; el._html = undefined; },
    get() { return el._text ?? ''; },
  });
  return el;
}
const els = new Map();
const document = {
  getElementById: (id) => {
    if (!els.has(id)) els.set(id, makeEl(id));
    return els.get(id);
  },
  querySelectorAll: () => [],
  createElement: () => makeEl('tmp'),
};

const api = new Function('document', `${script}
  ;return {render,isRecent,formatTime,bjDateKey,getCards:()=>cards};`)(document);

console.log('== the 24-hour window ==');
check(api.isRecent({ created_at: iso(t - 1 * HOUR) }) === true, '1 hour old is recent');
check(api.isRecent({ created_at: iso(t - 23 * HOUR) }) === true, '23 hours old is recent');
check(api.isRecent({ created_at: iso(t - 25 * HOUR) }) === false, '25 hours old is not recent');
check(api.isRecent({ created_at: null, fetched_at: iso(t - 2 * HOUR) }) === true,
  'no created_at falls back to fetched_at');
check(api.isRecent({ created_at: null, fetched_at: null }) === false,
  'no timestamp at all is not recent');
check(api.isRecent({}) === false, 'a row with neither field is not recent');

console.log('\n== the rendered sections ==');
api.render();
const newHtml = document.getElementById('cards-new').innerHTML;
const oldHtml = document.getElementById('cards-old').innerHTML;
check(newHtml.includes('一小时前发的') && newHtml.includes('二十三小时前发的'),
  'recent cards land in the 近 24 小时 section');
check(!newHtml.includes('二十五小时前发的') && !newHtml.includes('十天前发的'),
  'older cards do not leak into it');
check(oldHtml.includes('二十五小时前发的') && oldHtml.includes('十天前发的'),
  'older cards land in the other section');
check(newHtml.includes('无发布时间但刚抓到') && oldHtml.includes('无发布时间且抓到很久了'),
  'rows without created_at are split by fetched_at too');
check(newHtml.includes('近 24 小时') || html.includes('近 24 小时'),
  'the section heading says what it means', newHtml.slice(0, 80));

console.log('\n== only-recent payload shows the empty state ==');
const onlyRecent = cards.filter((c) => c.id === '1');
const script2 = inlineJs.replace(payload, JSON.stringify(onlyRecent));
const els2 = new Map();
const doc2 = {
  getElementById: (id) => {
    if (!els2.has(id)) els2.set(id, makeEl(id));
    return els2.get(id);
  },
  querySelectorAll: () => [],
  createElement: () => makeEl('tmp'),
};
const api2 = new Function('document', `${script2};return {render};`)(doc2);
api2.render();
check(doc2.getElementById('cards-old').innerHTML.includes('更早'),
  'an empty older section explains itself',
  doc2.getElementById('cards-old').innerHTML);

console.log('\n== the card clock is Beijing, like the date groups ==');
// 20:00 UTC is 04:00 the next day in Beijing. A browser-local formatting would
// print 20:00 on a UTC runner and disagree with the "📅" grouping above it.
const beijing = api.formatTime('2026-10-01T20:00:00Z');
check(beijing.includes('10/02') && beijing.includes('04:00'),
  'a UTC timestamp renders as its Beijing wall time', beijing);
const group = api.bjDateKey('2026-10-01T20:00:00Z');
check(group === '2026-10-02', 'and the day grouping agrees', group);

console.log();
console.log(failed ? `${failed} FAILURE(S)` : 'all page-JS checks passed');
process.exit(failed ? 1 : 0);
