#!/usr/bin/env python3
"""
linuxsb-daily fetcher
Scrapes multiple sites for AI 中转站福利 posts and emits a JSON lines file.
Sources:
  - linux.sb: /forum/2, /forum/8, /index.php?sort=lucky, /index.php?sort=card, /
  - baipiao.org: /bbs
  - nodeloc.com: /latest
  - linux.do: /c/welfare/36 (best-effort, often unreachable)
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser

import urllib.request
import ssl

BASE_LINUXSB = "https://linux.sb"
BASE_BAIPIAO = "https://baipiao.org"
BASE_NODELOC = "https://www.nodeloc.com"
BASE_LINUXDO = "https://linux.do"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; linuxsb-daily/1.0; +https://github.com/xiaopeng66/linuxsb-daily)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Permissive SSL context for sites with cert issues
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# Use Clash proxy for all HTTP requests
_PROXY_HOST = os.environ.get("CLASH_PROXY_HOST", "127.0.0.1")
_PROXY_PORT = int(os.environ.get("CLASH_PROXY_PORT", "7897"))
_PROXY_URL = f"http://{_PROXY_HOST}:{_PROXY_PORT}"


def fetch(url: str, timeout: int = 20) -> str:
    proxy_handler = urllib.request.ProxyHandler({"http": _PROXY_URL, "https": _PROXY_URL})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        if _PROXY_HOST not in (None, "", "127.0.0.1", "localhost"):
            raise
        # In GitHub Actions or when proxy is unavailable, retry direct
        direct_opener = urllib.request.build_opener()
        req2 = urllib.request.Request(url, headers=HEADERS)
        with direct_opener.open(req2, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")


CATEGORY_KEYWORDS = {
    "抽奖": ["抽奖", "盲盒", "中奖", "欧皇"],
    "兑换码": ["兑换码", "邀请码", "注册码", "code", "key", "cdk"],
    "公益站": ["公益", "免费使用", "零门槛", "免费", "公益站"],
    "额度赠送": ["送", "额度", "刀", "token", "credit", "$", "体验金", "积分"],
    "福利放送": ["福利", "鸡蛋", "羊毛", "白嫖"],
}


class LinuxSBHTMLParser(HTMLParser):
    """Extract /topic/N links from linux.sb listing pages."""
    def __init__(self):
        super().__init__()
        self.topics = []
        self._current_href = None
        self._in_topic_link = False

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href", "")
        if "/topic/" in href:
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
                    "url": f"{BASE_LINUXSB}/topic/{self._current_href}",
                })
            self._in_topic_link = False
            self._current_href = None


class BaipiaoHTMLParser(HTMLParser):
    """Extract /bbs/d/N-title links from baipiao.org/bbs listing pages."""
    def __init__(self):
        super().__init__()
        self.topics = []
        self._current_href = None
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href", "")
        m = re.search(r"/bbs/d/(\d+-[^\s\"#]+)", href)
        if m:
            self._current_href = m.group(0)
            self._in_link = True

    def handle_data(self, data):
        if self._in_link and self._current_href:
            title = data.strip()
            if title and len(title) > 2:
                self.topics.append({
                    "id": self._current_href,
                    "title": title,
                    "url": f"{BASE_BAIPIAO}{self._current_href}",
                })
            self._in_link = False
            self._current_href = None


class NodeLocHTMLParser(HTMLParser):
    """Extract /t/topic/N links from nodeloc.com listing pages."""
    def __init__(self):
        super().__init__()
        self.topics = []
        self._current_href = None
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href", "")
        m = re.search(r"/t/topic/(\d+)", href)
        if m:
            self._current_href = m.group(1)
            self._in_link = True

    def handle_data(self, data):
        if self._in_link and self._current_href:
            title = data.strip()
            if title and len(title) > 2:
                self.topics.append({
                    "id": self._current_href,
                    "title": title,
                    "url": f"{BASE_NODELOC}/t/topic/{self._current_href}",
                })
            self._in_link = False
            self._current_href = None


def parse_topics(html: str, parser_class) -> list:
    parser = parser_class()
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
    title = topic["title"]
    tags = []
    for tag, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in title:
                tags.append(tag)
                break
    tags = list(dict.fromkeys(tags))
    topic["tags"] = tags
    topic["score"] = len(tags)
    return topic


def fetch_linuxsb() -> list:
    sources = [
        ("linuxsb_福利放送", lambda: fetch(f"{BASE_LINUXSB}/forum/2?sort=post")),
        ("linuxsb_我要推广", lambda: fetch(f"{BASE_LINUXSB}/forum/8?sort=post")),
        ("linuxsb_抽奖", lambda: fetch(f"{BASE_LINUXSB}/index.php?sort=lucky")),
        ("linuxsb_发卡", lambda: fetch(f"{BASE_LINUXSB}/index.php?sort=card")),
        ("linuxsb_首页", lambda: fetch(f"{BASE_LINUXSB}/")),
    ]
    all_topics = []
    for name, fetcher in sources:
        try:
            html = fetcher()
            topics = parse_topics(html, LinuxSBHTMLParser)
            print(f"[fetch] {name}: {len(topics)} topics", file=sys.stderr)
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] {name} failed: {e}", file=sys.stderr)
    return all_topics


def fetch_baipiao() -> list:
    all_topics = []
    for page in range(1, 4):
        url = f"{BASE_BAIPIAO}/bbs/all?page={page}"
        try:
            html = fetch(url)
            topics = parse_topics(html, BaipiaoHTMLParser)
            print(f"[fetch] baipiao page {page}: {len(topics)} topics", file=sys.stderr)
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] baipiao page {page} failed: {e}", file=sys.stderr)
            break
    return all_topics


def fetch_nodeloc() -> list:
    all_topics = []
    for page in range(1, 4):
        url = f"{BASE_NODELOC}/latest?order=created&page={page}"
        try:
            html = fetch(url)
            topics = parse_topics(html, NodeLocHTMLParser)
            print(f"[fetch] nodeloc page {page}: {len(topics)} topics", file=sys.stderr)
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] nodeloc page {page} failed: {e}", file=sys.stderr)
            break
    return all_topics


def fetch_linuxdo() -> list:
    """Linux.do is often unreachable from some networks; skip gracefully."""
    topics = []
    try:
        html = fetch(f"{BASE_LINUXDO}/c/welfare/36", timeout=20)
        topics = parse_topics(html, LinuxSBHTMLParser)
        print(f"[fetch] linux.do: {len(topics)} topics", file=sys.stderr)
    except Exception as e:
        print(f"[warn] linux.do skipped: {e}", file=sys.stderr)
    return topics


def main():
    ap = argparse.ArgumentParser(description="Fetch welfare topics from multiple sources")
    ap.add_argument("--output", "-o", default="data/topics.jsonl")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    all_topics = []

    # Source 1-5: linux.sb
    all_topics.extend(fetch_linuxsb())
    # Source 6: baipiao.org
    all_topics.extend(fetch_baipiao())
    # Source 7: nodeloc.com
    all_topics.extend(fetch_nodeloc())
    # Source 8: linux.do (best-effort)
    all_topics.extend(fetch_linuxdo())

    # Deduplicate
    all_topics = deduplicate(all_topics)

    # Score and tag
    all_topics = [score_topic(t) for t in all_topics]

    # Sort by score desc, then by id (newer first)
    all_topics.sort(key=lambda t: (-t["score"], -int(re.search(r"\d+", t["id"]).group(0))))

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
