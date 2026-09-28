#!/usr/bin/env python3
"""
linuxsb-daily fetcher
Scrapes linux.sb for AI 中转站福利 posts and emits a JSON lines file.
Sources:
  - /forum/2  福利放送
  - /forum/8  我要推广
  - /index.php?sort=lucky 抽奖
  - /index.php?sort=card  发卡
  - /              首页最新帖子
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

import urllib.request

BASE = "https://linux.sb"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; linuxsb-daily/1.0; +https://github.com/1847531284/linuxsb-daily)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

CATEGORY_KEYWORDS = {
    "抽奖": ["抽奖", "盲盒", "中奖", "欧皇", "抽"],
    "兑换码": ["兑换码", "邀请码", "注册码", "码", "code"],
    "公益站": ["公益", "免费使用", "零门槛", "免费", "公益站"],
    "额度赠送": ["送", "额度", "刀", "token", "credit", "$", "体验金", "积分"],
    "福利放送": ["福利", "鸡蛋", "羊毛", "白嫖"],
}

class SimpleTopicParser(HTMLParser):
    """Extract topic links and titles from forum listing pages."""
    def __init__(self):
        super().__init__()
        self.topics = []
        self._current_href = None
        self._in_topic_link = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href", "")
        if tag == "a" and "/topic/" in href:
            # Normalize: keep only /topic/NUMBER
            m = re.search(r"/topic/(\d+)", href)
            if m:
                self._current_href = m.group(1)
                self._in_topic_link = True

    def handle_data(self, data):
        if self._in_topic_link and self._current_href:
            title = data.strip()
            if title and len(title) > 2:
                self.topics.append({
                    "id": self._current_href,
                    "title": title,
                    "url": f"{BASE}/topic/{self._current_href}",
                })
            self._in_topic_link = False
            self._current_href = None

def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")

def parse_topics(html: str) -> list:
    parser = SimpleTopicParser()
    parser.feed(html)
    return parser.topics

def deduplicate(topics: list) -> list:
    seen = set()
    out = []
    for t in topics:
        key = t["id"]
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out

def score_topic(topic: dict) -> dict:
    """Score and tag a topic based on title keywords."""
    title = topic["title"]
    tags = []
    for tag, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in title:
                tags.append(tag)
                break
    # Deduplicate tags while preserving order
    tags = list(dict.fromkeys(tags))
    score = len(tags)
    topic["tags"] = tags
    topic["score"] = score
    return topic

def fetch_forum(forum_id: int, sort: str = "post") -> list:
    url = f"{BASE}/forum/{forum_id}"
    if sort == "post":
        url += "?sort=post"
    elif sort == "comment":
        url += "?sort=comment"
    print(f"[fetch] {url}", file=sys.stderr)
    html = fetch(url)
    return parse_topics(html)

def fetch_index(sort: str = "post") -> list:
    if sort == "lucky":
        url = f"{BASE}/index.php?sort=lucky"
    elif sort == "card":
        url = f"{BASE}/index.php?sort=card"
    else:
        url = BASE + "/"
    print(f"[fetch] {url}", file=sys.stderr)
    html = fetch(url)
    return parse_topics(html)

def main():
    parser = argparse.ArgumentParser(description="Fetch linux.sb welfare topics")
    parser.add_argument("--output", "-o", default="data/topics.jsonl")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    all_topics = []

    # Source 1: 福利放送
    all_topics.extend(fetch_forum(2, "post"))
    # Source 2: 我要推广
    all_topics.extend(fetch_forum(8, "post"))
    # Source 3: 抽奖
    all_topics.extend(fetch_index("lucky"))
    # Source 4: 发卡
    all_topics.extend(fetch_index("card"))
    # Source 5: 首页最新
    all_topics.extend(fetch_index("post"))

    # Deduplicate
    all_topics = deduplicate(all_topics)

    # Score and tag
    all_topics = [score_topic(t) for t in all_topics]

    # Sort by score desc, then by id (newer first as tiebreaker)
    all_topics.sort(key=lambda t: (-t["score"], -int(t["id"])))

    # Limit
    all_topics = all_topics[: args.limit]

    # Add metadata
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for t in all_topics:
        out.append({
            "id": t["id"],
            "title": t["title"],
            "url": t["url"],
            "tags": t["tags"],
            "score": t["score"],
            "fetched_at": now,
        })

    with open(args.output, "w", encoding="utf-8") as f:
        for item in out:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"[done] wrote {len(out)} topics to {args.output}", file=sys.stderr)

if __name__ == "__main__":
    main()
