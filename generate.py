#!/usr/bin/env python3
"""Generate a single-page HTML site from topics.jsonl."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", default="data/topics.jsonl")
    ap.add_argument("--output", "-o", default="docs/index.html")
    ap.add_argument("--repo", default="xiaopeng66/linuxsb-daily")
    args = ap.parse_args()

    topics = load_topics(args.input)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    card_parts = []
    for t in topics:
        tags_html = "".join(
            '<span class="tag tag-' + esc(tag) + '">' + esc(tag) + "</span>"
            for tag in t.get("tags", [])
        )
        card_parts.append(
            '<a class="card" href="' + esc(t["url"]) + '" target="_blank" rel="noopener">'
            "<div class=\"card-title\">" + esc(t["title"]) + "</div>"
            "<div class=\"card-meta\">" + tags_html
            + '<span class="score">匹配度 ' + str(t.get("score", 0)) + "</span></div>"
            "</a>"
        )
    cards_html = "\n".join(card_parts)
    cards_json = json.dumps(topics, ensure_ascii=False)

    html = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>LinuxSB 每日福利站</title><style>"
        "*{box-sizing:border-box;margin:0;padding:0}"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC',sans-serif;"
        "background:linear-gradient(135deg,#1e1e2e,#2d2d44);color:#e6e6e6;min-height:100vh;padding:20px}"
        ".container{max-width:960px;margin:0 auto}"
        "header{text-align:center;padding:30px 0}"
        "header h1{font-size:2rem;background:linear-gradient(90deg,#ff6b9d,#c44dff,#6b9dff);"
        "-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}"
        "header p{color:#a0a0b8;font-size:.9rem}"
        ".stats{display:flex;justify-content:center;gap:20px;flex-wrap:wrap;margin-bottom:20px}"
        ".stat-badge{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);"
        "padding:6px 14px;border-radius:20px;font-size:.85rem;color:#b0b0c8}"
        ".filters{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-bottom:24px}"
        ".filter-btn{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);"
        "color:#d0d0e0;padding:6px 14px;border-radius:18px;cursor:pointer;font-size:.85rem;transition:.2s}"
        ".filter-btn:hover,.filter-btn.active{background:linear-gradient(135deg,#ff6b9d33,#c44dff33);"
        "border-color:#c44dff88;color:#fff}"
        ".cards{display:grid;gap:12px}"
        ".card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);"
        "border-radius:12px;padding:16px;transition:.2s;cursor:pointer;text-decoration:none;color:inherit;display:block}"
        ".card:hover{background:rgba(255,255,255,.07);border-color:rgba(196,77,255,.4);transform:translateY(-1px)}"
        ".card-title{font-size:1rem;font-weight:600;color:#f0f0ff;margin-bottom:8px;line-height:1.4}"
        ".card-meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center}"
        ".tag{font-size:.75rem;padding:2px 10px;border-radius:10px;font-weight:500}"
        ".tag-抽奖{background:#ff6b9d33;color:#ff9dbf}.tag-兑换码{background:#ffb34733;color:#ffd080}"
        ".tag-公益站{background:#4ade8033;color:#86efac}.tag-额度赠送{background:#60a5fa33;color:#93c5fd}"
        ".tag-福利放送{background:#c084fc33;color:#d8b4fe}"
        ".score{font-size:.75rem;color:#808090;margin-left:auto}"
        "footer{text-align:center;padding:30px 0 10px;color:#606070;font-size:.8rem}"
        "footer a{color:#8888a0;text-decoration:none}.empty{text-align:center;padding:60px 20px;color:#707080}"
        "</style></head><body><div class=\"container\"><header>"
        "<h1>🎁 LinuxSB 每日福利站</h1>"
        "<p>自动聚合 linux.sb 的 AI 中转站福利 · 抽奖 · 兑换码 · 公益站 · 额度赠送</p>"
        "</header><div class=\"stats\">"
        '<span class="stat-badge">📊 共 ' + str(len(topics)) + ' 条</span>'
        '<span class="stat-badge">🕐 更新于 ' + esc(now) + '</span>'
        '<span class="stat-badge">📡 数据来源 linux.sb</span>'
        "</div><div class=\"filters\" id=\"filters\">"
        '<button class="filter-btn active" data-filter="all">全部</button>'
        '<button class="filter-btn" data-filter="抽奖">🎲 抽奖</button>'
        '<button class="filter-btn" data-filter="兑换码">🎫 兑换码</button>'
        '<button class="filter-btn" data-filter="公益站">💝 公益站</button>'
        '<button class="filter-btn" data-filter="额度赠送">💰 额度赠送</button>'
        '<button class="filter-btn" data-filter="福利放送">🎉 福利放送</button>'
        "</div><div class=\"cards\" id=\"cards\">" + cards_html + "</div><footer>"
        '<p>数据来源于 <a href="https://linux.sb" target="_blank">linux.sb</a> · 由 '
        '<a href="https://github.com/' + esc(args.repo) + '" target="_blank">'
        + esc(args.repo) + "</a> 自动更新</p>"
        '<p style="margin-top:4px;">⚠️ 本站仅做信息聚合，不保证链接有效性和安全性，请自行甄别</p>'
        "</footer></div><script>"
        "const cards=" + cards_json + ";"
        "const container=document.getElementById('cards');"
        "const filters=document.querySelectorAll('.filter-btn');"
        "function render(filter){"
        "const filtered=filter==='all'?cards:cards.filter(c=>c.tags.includes(filter));"
        "if(!filtered.length){container.innerHTML='<div class=\"empty\">该分类下暂无内容</div>';return;}"
        "container.innerHTML=filtered.map(card=>`"
        '<a class="card" href="${escapeAttr(card.url)}" target="_blank" rel="noopener">'
        '<div class="card-title">${escapeHtml(card.title)}</div>'
        '<div class="card-meta">${card.tags.map(t=>`<span class="tag tag-${escapeAttr(t)}">${escapeHtml(t)}</span>`).join("")}'
        '<span class="score">匹配度 ${card.score}</span></div></a>'
        '`).join("");}'
        "function escapeHtml(s){const div=document.createElement('div');div.textContent=s;return div.innerHTML;}"
        "function escapeAttr(s){return s.replace(/\"/g,'&quot;').replace(/'/g,'&#39;');}"
        "filters.forEach(btn=>{btn.addEventListener('click',()=>{"
        "filters.forEach(b=>b.classList.remove('active'));btn.classList.add('active');render(btn.dataset.filter);"
        "});});render('all');</script></body></html>"
    )

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    print("[done] wrote {} cards to {}".format(len(topics), args.output), file=sys.stderr)


if __name__ == "__main__":
    main()
