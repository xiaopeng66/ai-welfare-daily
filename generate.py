#!/usr/bin/env python3
"""Generate a single-page HTML site from topics.jsonl."""
import argparse
import json
import os
import sys
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


def _fmt_time(iso):
    if not iso:
        return "未知时间"
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.strftime("%m-%d %H:%M")
    except Exception:
        return iso[:16] if iso else "未知时间"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", default="data/topics.jsonl")
    ap.add_argument("--output", "-o", default="docs/index.html")
    ap.add_argument("--repo", default="xiaopeng66/linuxsb-daily")
    args = ap.parse_args()

    topics = load_topics(args.input)
    bj = timezone(timedelta(hours=8))
    now = datetime.now(bj).strftime("%Y-%m-%d %H:%M")

    # Build cards HTML
    card_parts = []
    for t in topics:
        tags_html = "".join(
            '<span class="tag tag-' + esc(tag) + '">' + esc(tag) + "</span>"
            for tag in t.get("tags", [])
        )
        source = t.get("source", "")
        source_label = {
            "linuxsb_福利放送": "linux.sb 福利放送",
            "linuxsb_我要推广": "linux.sb 推广",
            "linuxsb_抽奖": "linux.sb 抽奖",
            "linuxsb_发卡": "linux.sb 发卡",
            "linuxsb_首页": "linux.sb 首页",
        }.get(source, source)
        card_parts.append(
            '<a class="card" href="' + esc(t["url"]) + '" target="_blank" rel="noopener">'
            "<div class=\"card-title\">" + esc(t["title"]) + "</div>"
            "<div class=\"card-meta\">" + tags_html + "</div>"
            "<div class=\"card-footer\">"
            + '<span class="source">' + esc(source_label) + "</span>"
            + '<span class="score">匹配度 ' + str(t.get("score", 0)) + "</span>"
            + '<span class="post-time">' + esc(_fmt_time(t.get("created_at") or t.get("fetched_at"))) + "</span>"
            + "</div></a>"
        )
    cards_html = "\n".join(card_parts)
    cards_json = json.dumps(topics, ensure_ascii=False)

    html = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
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
        "border-radius:12px;padding:14px;transition:.2s;cursor:pointer;text-decoration:none;color:inherit;display:block}"
        ".card:hover{background:rgba(255,255,255,.07);border-color:rgba(196,77,255,.4);transform:translateY(-1px)}"
        ".card-title{font-size:.95rem;font-weight:600;color:#f0f0ff;margin-bottom:6px;line-height:1.4}"
        ".card-meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center}"
        ".tag{font-size:.72rem;padding:2px 8px;border-radius:10px;font-weight:500}"
        ".tag-中转站{background:#60a5fa33;color:#93c5fd}.tag-公益站{background:#4ade8033;color:#86efac}.tag-鸡蛋{background:#ffb34733;color:#ffd080}"
        ".tag-兑换码{background:#ff6b9d33;color:#ff9dbf}.tag-额度{background:#c084fc33;color:#d8b4fe}.tag-体验金{background:#f472b633;color:#f9a8d4}"
        ".tag-抽奖{background:#34d39933;color:#6ee7b7}"
        ".source{font-size:.72rem;color:#9090a0;padding:2px 6px;border-radius:6px;background:rgba(255,255,255,.05)}"
        ".score{font-size:.72rem;color:#808090;margin-left:4px}"
        ".post-time{font-size:.72rem;color:#a0a0a0;margin-left:auto;padding:2px 6px;border-radius:6px}"
        ".card-footer{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,.06)}"
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
        ".card{padding:12px}"
        ".card-title{font-size:.88rem}"
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
        '<button class=\"filter-btn\" data-filter=\"nodeloc\">nodeloc.com</button></div>'
        '<div class="filter-row" data-group="category"><span class="filter-label">分类：</span>'
        '<button class="filter-btn active" data-filter="all">全部</button>'
        '<button class="filter-btn" data-filter="中转站">🔄 中转站</button>'
        '<button class="filter-btn" data-filter="公益站">💝 公益站</button>'
        '<button class="filter-btn" data-filter="鸡蛋">🥚 鸡蛋</button>'
        '<button class="filter-btn" data-filter="兑换码">🎫 兑换码</button>'
        '<button class="filter-btn" data-filter="额度">💰 额度</button>'
        '<button class="filter-btn" data-filter="体验金">🎁 体验金</button>'
        '<button class="filter-btn" data-filter="抽奖">🎲 抽奖</button></div>'
        "</div><div class=\"cards\" id=\"cards\">" + cards_html + "</div><footer>"
        '<p>数据来源于 <a href="https://linux.sb" target="_blank">linux.sb</a> / '
        '<a href="https://baipiao.org/bbs" target="_blank">baipiao.org</a> / '
        '<a href="https://www.nodeloc.com/latest" target="_blank">nodeloc.com</a> · 由 '
        '<a href="https://github.com/' + esc(args.repo) + '" target="_blank">'
        + esc(args.repo) + "</a> 自动更新</p>"
        '<p style="margin-top:4px;">⚠️ 本站仅做信息聚合，不保证链接有效性和安全性，请自行甄别</p>'
        "</footer></div><script>"
        "const cards=" + cards_json + ";"
        "const container=document.getElementById('cards');"
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
        "}"
        "if(categoryFilter!=='all'){if(!c.tags.includes(categoryFilter)) return false;}"
        "return true;});"
        "return filtered;}"

        "function render(){"
        "let filtered=getFiltered();"
        "if(sortMode==='time'){filtered=[...filtered].sort((a,b)=>{"
        "const at=a.created_at||a.fetched_at||'';const bt=b.created_at||b.fetched_at||'';"
        "return bt.localeCompare(at);});}"
        "else{filtered=[...filtered].sort((a,b)=>(b.score||0)-(a.score||0)||(b.fetched_at||'').localeCompare(a.fetched_at||''));}"
        "if(!filtered.length){container.innerHTML='<div class=\"empty\">该筛选下暂无内容</div>';return;}"
        "container.innerHTML=filtered.map(card=>`"
        '<a class="card" href="${escapeAttr(card.url)}" target="_blank" rel="noopener">'
        '<div class="card-title">${escapeHtml(card.title)}</div>'
        '<div class="card-meta">${card.tags.map(t=>`<span class="tag tag-${escapeAttr(t)}">${escapeHtml(t)}</span>`).join("")}</div>'
        '<div class="card-footer"><span class="source">${escapeHtml(card.source||"")}</span>'
        '<span class="score">匹配度 ${card.score}</span>'
        '<span class="post-time">${formatTime(card.created_at||card.fetched_at)}</span></div></a>'
        '`).join("");}'

        "function escapeHtml(s){const div=document.createElement('div');div.textContent=s;return div.innerHTML;}"
        "function escapeAttr(s){return s.replace(/\"/g,'&quot;').replace(/'/g,'&#39;');}"
        "function formatTime(iso){if(!iso) return '未知时间';try{const d=new Date(iso);return d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}catch(e){return iso;}}"
        "filters.forEach(btn=>{btn.addEventListener('click',()=>{"
        "if(btn.dataset.sort){"
        "sortMode=btn.dataset.sort;"
        "filters.forEach(b=>{if(b.dataset.sort){b.classList.remove('active');}"
        "else{b.classList.remove('active');}});"
        "btn.classList.add('active');"
        "render();return;}"
        "const group=btn.closest('.filter-row');"
        "if(group && group.dataset.group==='source'){sourceFilter=btn.dataset.filter;"
        "group.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');render();return;}"
        "if(group && group.dataset.group==='category'){categoryFilter=btn.dataset.filter;"
        "group.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');render();return;}"
        "});});render();</script></body></html>"
    )

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    print("[done] wrote {} cards to {}".format(len(topics), args.output), file=sys.stderr)


if __name__ == "__main__":
    main()
