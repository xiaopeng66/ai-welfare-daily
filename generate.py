#!/usr/bin/env python3
"""Generate a single-page HTML site from topics.jsonl."""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from html import escape


def load_topics(path):
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def esc(s):
    return escape(s)


def _atomic_write(path, text):
    """Temp file + os.replace: a killed run must not leave a half-written page."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _parse_sortable(created_at):
    if not created_at:
        return 0
    try:
        s = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception:
        return 0


def _data_updated(topics, store_path, bj):
    """Newest fetched_at in the store, formatted in Beijing time.

    fetched_at is bumped only when a row actually changes (see fetch.py), so its
    maximum is "when the data last moved". Using it instead of the wall clock
    keeps the page a pure function of the store: a run with no news renders a
    byte-identical file, so there is nothing to commit or redeploy.
    """
    newest = 0.0
    for t in topics:
        newest = max(newest, _parse_sortable(t.get("fetched_at")))
    if not newest:
        try:
            newest = os.path.getmtime(store_path)
        except OSError:
            newest = datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(newest, bj).strftime("%Y-%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", default="data/topics.jsonl")
    ap.add_argument("--output", "-o", default="docs/index.html")
    ap.add_argument("--repo", default="xiaopeng66/linuxsb-daily")
    args = ap.parse_args()

    topics = load_topics(args.input)
    bj = timezone(timedelta(hours=8))
    # "更新于" = when the DATA last changed, not when we happened to render.
    now = _data_updated(topics, args.input, bj)

    # json.dumps leaves '<' untouched, so a scraped title containing
    # "</script>" would break out of the embedding <script> block (stored XSS
    # on the public Pages site). Escape the HTML-sensitive characters: the JSON
    # parser still yields the original strings.
    cards_json = (
        json.dumps(topics, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )

    html = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="description" content="聚合 linux.sb / baipiao.org / nodeloc.com / linux.do 的 AI 中转站、公益站、鸡蛋、兑换码、额度、体验金与抽奖福利帖，每日 08:00 与 20:00（UTC+8）自动更新，可按来源、分类、时间筛选。">'
        '<meta name="theme-color" content="#1e1e2e">'
        '<title>AI 福利日报 - 中转站/兑换码/公益站/额度/鸡蛋</title><style>'
        "*{box-sizing:border-box;margin:0;padding:0}"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC',sans-serif;"
        "background:linear-gradient(135deg,#1e1e2e,#2d2d44);color:#e6e6e6;min-height:100vh;padding:16px}"
        ".container{max-width:960px;margin:0 auto;padding:0 8px}"
        "header{text-align:center;padding:24px 0}"
        "header h1{font-size:1.6rem;background:linear-gradient(90deg,#ff6b9d,#c44dff,#6b9dff);"
        "-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px}"
        "header p{color:#a0a0b8;font-size:.82rem;padding:0 12px}"
        ".stats{display:flex;justify-content:center;gap:10px;flex-wrap:wrap;margin-bottom:16px}"
        ".stat-badge{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);"
        "padding:5px 12px;border-radius:18px;font-size:.78rem;color:#b0b0c8}"
        ".filters{display:flex;flex-direction:column;align-items:center;gap:8px;margin-bottom:20px}"
        ".filter-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;justify-content:center}"
        ".filter-label{font-size:.75rem;color:#9090a8;margin-right:2px;min-width:2.5em}"
        ".filter-btn{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);"
        "color:#d0d0e0;padding:5px 12px;border-radius:16px;cursor:pointer;font-size:.78rem;transition:.2s;"
        "touch-action:manipulation;-webkit-tap-highlight-color:transparent}"
        ".filter-btn:hover,.filter-btn.active{background:linear-gradient(135deg,#ff6b9d33,#c44dff33);"
        "border-color:#c44dff88;color:#fff}"
        ".cards{display:grid;gap:10px;grid-template-columns:1fr}"
        "@media(min-width:768px){.cards{grid-template-columns:repeat(2,1fr)}}"
        ".card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);"
        "border-radius:12px;padding:14px;transition:.2s;cursor:pointer;text-decoration:none;color:inherit;display:flex;flex-direction:column;gap:8px}"
        ".card:hover{background:rgba(255,255,255,.07);border-color:rgba(196,77,255,.4);transform:translateY(-1px)}"
        ".card-title{font-size:.95rem;font-weight:600;color:#f0f0ff;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;flex:1 1 auto}"
        ".card-meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center}"
        ".card-footer{margin-top:auto;display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding-top:8px;border-top:1px solid rgba(255,255,255,.06)}"
        ".tag{font-size:.72rem;padding:2px 8px;border-radius:10px;font-weight:500}"
        ".tag-中转站{background:#60a5fa33;color:#93c5fd}.tag-公益站{background:#4ade8033;color:#86efac}.tag-鸡蛋{background:#ffb34733;color:#ffd080}"
        ".tag-兑换码{background:#ff6b9d33;color:#ff9dbf}.tag-额度{background:#c084fc33;color:#d8b4fe}.tag-体验金{background:#f472b633;color:#f9a8d4}"
        ".tag-抽奖{background:#34d39933;color:#6ee7b7}"
        ".source{font-size:.72rem;color:#9090a0;padding:2px 6px;border-radius:6px;background:rgba(255,255,255,.05)}"
        ".score{font-size:.72rem;color:#808090;margin-left:4px}"
        ".post-time{font-size:.72rem;color:#a0a0a0;margin-left:auto;padding:2px 6px;border-radius:6px}"
        ".card-footer{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,.06)}"
        ".section-header{font-size:.85rem;font-weight:600;padding:8px 4px;color:#c0c0d0;border-bottom:1px solid rgba(255,255,255,.08);margin-bottom:4px}"
        ".new-section{color:#ff9dbf}.old-section{color:#9090a0}"
        "footer{text-align:center;padding:24px 0 10px;color:#606070;font-size:.78rem}"
        "footer a{color:#8888a0;text-decoration:none}.empty{text-align:center;padding:50px 16px;color:#707080}"
        "@media(max-width:600px){"
        "body{padding:12px}"
        ".container{padding:0 4px}"
        "header h1{font-size:1.35rem}"
        "header p{font-size:.78rem}"
        ".stat-badge{font-size:.72rem;padding:4px 10px}"
        ".filter-btn{padding:5px 10px;font-size:.75rem;border-radius:14px}"
        ".cards{gap:8px}"
        ".card{padding:12px;display:flex;flex-direction:column;gap:6px}"
        ".card-title{font-size:.88rem;flex:1 1 auto}"
        ".card-footer{margin-top:auto;padding-top:6px;border-top:1px solid rgba(255,255,255,.06)}"
        ".tag,.source,.score,.post-time{font-size:.68rem}"
        "}"
        "</style></head><body><div class=\"container\"><header>"
        "<h1>🎁 AI 福利日报</h1>"
        "<p>聚合多站 AI 福利信息：中转站、公益站、鸡蛋、兑换码、额度、体验金、抽奖</p>"
        "</header><div class=\"stats\">"
        '<span class="stat-badge">📊 共 ' + str(len(topics)) + ' 条</span>'
        '<span class="stat-badge">🕐 更新于 ' + esc(now) + '</span>'
        '<span class="stat-badge">📡 多源聚合</span>'
        "</div><div class=\"filters\" id=\"filters\">"
        '<div class=\"filter-row\" data-group=\"sort\"><span class=\"filter-label\">排序：</span>'
        '<button class=\"filter-btn active\" data-sort=\"relevance\">按匹配度</button>'
        '<button class=\"filter-btn\" data-sort=\"time\">按时间</button></div>'
        '<div class=\"filter-row\" data-group=\"source\"><span class=\"filter-label\">来源：</span>'
        '<button class=\"filter-btn active\" data-filter=\"all\">全部</button>'
        '<button class=\"filter-btn\" data-filter=\"linuxsb\">linux.sb</button>'
        '<button class=\"filter-btn\" data-filter=\"baipiao\">baipiao.org</button>'
        '<button class=\"filter-btn\" data-filter=\"nodeloc\">nodeloc.com</button>'
        '<button class=\"filter-btn\" data-filter=\"linuxdo\">linux.do</button></div>'
        '<div class="filter-row" data-group="category"><span class="filter-label">分类：</span>'
        '<button class="filter-btn active" data-filter="all">全部</button>'
        '<button class="filter-btn" data-filter="中转站">🔄 中转站</button>'
        '<button class="filter-btn" data-filter="公益站">💝 公益站</button>'
        '<button class="filter-btn" data-filter="鸡蛋">🥚 鸡蛋</button>'
        '<button class="filter-btn" data-filter="兑换码">🎫 兑换码</button>'
        '<button class="filter-btn" data-filter="额度">💰 额度</button>'
        '<button class="filter-btn" data-filter="体验金">🎁 体验金</button>'
        '<button class="filter-btn" data-filter="抽奖">🎲 抽奖</button></div>'
        "</div><div id=\"cards\">"
        '<div class="section-header new-section" id="header-new">🆕 本次更新后</div>'
        '<div class="cards" id="cards-new"></div>'
        '<div class="section-header old-section" id="header-old">📋 之前已有</div>'
        '<div class="cards" id="cards-old"></div>'
        '<div id="cards-time" style="display:none"></div>'
        "</div><footer>"
        '<p>数据来源于 <a href="https://linux.sb" target="_blank">linux.sb</a> / '
        '<a href="https://baipiao.org/bbs" target="_blank">baipiao.org</a> / '
        '<a href="https://www.nodeloc.com/latest" target="_blank">nodeloc.com</a> / '
        '<a href="https://linux.do/c/welfare/36" target="_blank">linux.do</a> · 由 '
        '<a href="https://github.com/' + esc(args.repo) + '" target="_blank">'
        + esc(args.repo) + "</a> 自动更新</p>"
        '<p style="margin-top:4px;">⚠️ 本站仅做信息聚合，不保证链接有效性和安全性，请自行甄别</p>'
        "</footer></div><script>"
        "const cards=" + cards_json + ";"
        "const containerNew=document.getElementById('cards-new');"
        "const containerOld=document.getElementById('cards-old');"
        "const filters=document.querySelectorAll('.filter-btn');"
        "let sortMode='relevance';"
        "let sourceFilter='all';"
        "let categoryFilter='all';"

        "function getFiltered(){"
        "const filtered=cards.filter(c=>{"
        "if(sourceFilter!=='all'){"
        "if(sourceFilter==='linuxsb'){if(!(c.source&&c.source.startsWith('linuxsb'))) return false;}"
        "else if(sourceFilter==='baipiao'){if(!(c.source&&c.source.startsWith('baipiao'))) return false;}"
        "else if(sourceFilter==='nodeloc'){if(!(c.source&&c.source.startsWith('nodeloc'))) return false;}"
        "else if(sourceFilter==='linuxdo'){if(!(c.source&&c.source.startsWith('linuxdo'))) return false;}"
        "}"
        "if(categoryFilter!=='all'){if(!(c.tags||[]).includes(categoryFilter)) return false;}"
        "return true;});"
        "return filtered;}"

        "function sourceLabel(s){"
        "if(!s) return '';"
        "if(s.indexOf('linuxsb')===0) return 'linux.sb';"
        "if(s.indexOf('baipiao')===0) return 'baipiao.org';"
        "if(s.indexOf('nodeloc_welfare')===0) return 'nodeloc 福利';"
        "if(s.indexOf('nodeloc')===0) return 'nodeloc.com';"
        "if(s.indexOf('linuxdo')===0) return 'linux.do';"
        "return s;}"

        "function renderCard(card){"
        # Every field is coerced. render() maps over ALL cards in one
        # expression, so a single row missing `tags` or `score` used to throw
        # mid-render and leave both sections empty (a blank page) instead of
        # one odd-looking card.
        "return '<a class=\"card\" href=\"'+escapeAttr(card.url)+'\" target=\"_blank\" rel=\"noopener\">'"
        "+'<div class=\"card-title\">'+escapeHtml(card.title)+'</div>'"
        "+'<div class=\"card-meta\">'+(card.tags||[]).map(t=>'<span class=\"tag tag-'+escapeAttr(t)+'\">'+escapeHtml(t)+'</span>').join('')+'</div>'"
        "+'<div class=\"card-footer\"><span class=\"source\">'+escapeHtml(sourceLabel(card.source))+'</span>'"
        "+'<span class=\"score\">匹配度 '+(card.score||0)+'</span>'"
        "+'<span class=\"post-time\">'+formatTime(card.created_at||card.fetched_at)+'</span></div></a>';}"

        "function render(){"
        "const containerNew=document.getElementById('cards-new');"
        "const containerOld=document.getElementById('cards-old');"
        "const containerTime=document.getElementById('cards-time');"
        "const headerNew=document.getElementById('header-new');"
        "const headerOld=document.getElementById('header-old');"
        "let filtered=getFiltered();"
        "if(sortMode==='time'){filtered=[...filtered].sort((a,b)=>{"
        "const at=Date.parse(a.created_at||a.fetched_at||0);const bt=Date.parse(b.created_at||b.fetched_at||0);"
        "return bt-at;});"
        "const groups={};filtered.forEach(c=>{"
        "const key=bjDateKey(c.created_at||c.fetched_at);"
        "if(!groups[key]) groups[key]=[];groups[key].push(c);});"
        "const keys=Object.keys(groups).sort((a,b)=>b.localeCompare(a));"
        "containerNew.style.display='none';containerOld.style.display='none';headerNew.style.display='none';headerOld.style.display='none';"
        "containerTime.style.display='';"
        "containerTime.innerHTML=keys.length?keys.map(k=>'<div class=\\\"section-header\\\">📅 '+escapeHtml(k)+'</div><div class=\\\"cards\\\">'+groups[k].map(renderCard).join('')+'</div>').join(''):'<div class=\\\"empty\\\">该筛选下暂无内容</div>';"
        "}else{"
        "filtered=[...filtered].sort((a,b)=>(b.score||0)-(a.score||0)||(Date.parse(b.fetched_at||0)-Date.parse(a.fetched_at||0)));"
        "containerTime.style.display='none';containerNew.style.display='';containerOld.style.display='';headerNew.style.display='';headerOld.style.display='';"
        "const newCards=filtered.filter(c=>c.is_new);"
        "const oldCards=filtered.filter(c=>!c.is_new);"
        "if(!newCards.length){containerNew.innerHTML='<div class=\\\"empty\\\">该筛选下暂无新帖</div>';}"
        "else{containerNew.innerHTML=newCards.map(renderCard).join('');}"
        "if(!oldCards.length){containerOld.innerHTML='<div class=\\\"empty\\\">该筛选下暂无旧帖</div>';}"
        "else{containerOld.innerHTML=oldCards.map(renderCard).join('');}"
        "if(!filtered.length){containerNew.innerHTML='<div class=\\\"empty\\\">该筛选下暂无内容</div>';containerOld.innerHTML='';}"
        "}"
        "}"
        "function escapeHtml(s){const div=document.createElement('div');div.textContent=(s==null?'':s);return div.innerHTML;}"
        "function escapeAttr(s){return String(s==null?'':s).replace(/\"/g,'&quot;').replace(/'/g,'&#39;');}"
        "function formatTime(iso){if(!iso) return '未知时间';try{const d=new Date(iso);return d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}catch(e){return iso;}}"
        # Group headers must name the Beijing calendar date, not the UTC one:
        # created_at is UTC, so slicing the raw ISO string filed every post
        # published 16:00-24:00 UTC (00:00-08:00 CST) under the previous day and
        # contradicted the time the card itself shows (measured 19/161 rows).
        "function bjDateKey(iso){if(!iso) return '未知日期';const d=new Date(iso);if(isNaN(d.getTime())) return '未知日期';return new Date(d.getTime()+8*3600*1000).toISOString().slice(0,10);}"
        "filters.forEach(btn=>{btn.addEventListener('click',()=>{"
        "if(btn.dataset.sort){"
        "sortMode=btn.dataset.sort;"
        "filters.forEach(b=>{if(b.dataset.sort){b.classList.remove('active');}});"
        "btn.classList.add('active');"
        "render();return;}"
        "const group=btn.closest('.filter-row');"
        "if(group && group.dataset.group==='source'){sourceFilter=btn.dataset.filter;"
        "group.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');render();return;}"
        "if(group && group.dataset.group==='category'){categoryFilter=btn.dataset.filter;"
        "group.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');render();return;}"
        "});});"
        "render();</script></body></html>"
    )

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    _atomic_write(args.output, html)

    print("[done] wrote {} cards to {}".format(len(topics), args.output), file=sys.stderr)


if __name__ == "__main__":
    main()
