#!/usr/bin/env python3
"""
linuxsb-daily fetcher
Scrapes multiple sites for AI 中转站福利 posts and emits a JSON lines file.
Sources:
  - linux.sb: /forum/2, /forum/8, /index.php?sort=lucky, /index.php?sort=card, /
  - baipiao.org: /bbs/api/discussions
  - nodeloc.com: /latest.json
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
    except Exception:
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

# Strong relevance keywords; title must contain at least one
RELEVANCE_KEYWORDS = [
    "中转站", "公益站", "鸡蛋", "兑换码", "额度", "体验金",
]


class LinuxSBHTMLParser(HTMLParser):
    """Extract /topic/N links and post times from linux.sb listing pages."""

    def __init__(self):
        super().__init__()
        self.topics = []
        self._current_href = None
        self._in_topic_link = False
        self._current_time = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href", "")
            if "/topic/" in href:
                m = re.search(r"/topic/(\d+)", href)
                if m:
                    self._current_href = m.group(1)
                    self._in_topic_link = True
        elif tag == "span" and self._in_topic_link:
            attrs_dict = dict(attrs)
            if "data-performance-time" in attrs_dict:
                self._current_time = attrs_dict["data-performance-time"]

    def handle_data(self, data):
        if self._in_topic_link and self._current_href:
            title = data.strip()
            if title and len(title) > 2:
                self.topics.append({
                    "id": self._current_href,
                    "title": title,
                    "url": f"{BASE_LINUXSB}/topic/{self._current_href}",
                    "created_at": self._parse_unix(self._current_time),
                })
            self._in_topic_link = False
            self._current_href = None
            self._current_time = None

    @staticmethod
    def _parse_unix(ts):
        if not ts:
            return None
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        except Exception:
            return None


class BaipiaoHTMLParser(HTMLParser):
    """Fallback parser for baipiao.org HTML listings."""

    def __init__(self):
        super().__init__()
        self.topics = []
        self._current_href = None
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href") or ""
        m = re.search(r'/bbs/d/(\d+-[^\s"#]+)', href)
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
                    "created_at": None,
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
        href = attrs_dict.get("href") or ""
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
                    "created_at": None,
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
        ("linuxsb_福利放送", f"{BASE_LINUXSB}/forum/2?sort=post"),
        ("linuxsb_我要推广", f"{BASE_LINUXSB}/forum/8?sort=post"),
        ("linuxsb_抽奖", f"{BASE_LINUXSB}/index.php?sort=lucky"),
        ("linuxsb_发卡", f"{BASE_LINUXSB}/index.php?sort=card"),
        ("linuxsb_首页", f"{BASE_LINUXSB}/"),
    ]
    pattern = re.compile(r'href=["\'](/topic/(\d+))["\'][^>]*>(.*?)</a>.*?<span[^>]*data-performance-time="(\d+)"', re.S)
    all_topics = []
    for name, url in sources:
        try:
            html = fetch(url)
            topics = []
            seen = set()
            for topic_id, num, title, ts in pattern.findall(html):
                topic_id = topic_id.split("/")[-1]
                if topic_id in seen:
                    continue
                seen.add(topic_id)
                clean_title = re.sub(r"<[^>]+>", "", title).strip()
                if not clean_title or len(clean_title) <= 2:
                    continue
                created_at = None
                try:
                    created_at = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
                except Exception:
                    pass
                topics.append({
                    "id": topic_id,
                    "title": clean_title,
                    "url": f"{BASE_LINUXSB}/topic/{topic_id}",
                    "created_at": created_at,
                })
            for t in topics:
                t["source"] = name
            print(f"[fetch] {name}: {len(topics)} topics", file=sys.stderr)
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] {name} failed: {e}", file=sys.stderr)
    return all_topics


def fetch_baipiao() -> list:
    all_topics = []
    for page in range(1, 4):
        api_url = f"{BASE_BAIPIAO}/bbs/api/discussions?page={page}&sort=-createdAt"
        html_url = f"{BASE_BAIPIAO}/bbs/all?page={page}"
        try:
            try:
                html = fetch(api_url)
                data = json.loads(html)
                topics = []
                for item in data.get("data", []):
                    attr = item.get("attributes", {})
                    topic_id = item.get("id", "")
                    slug = attr.get("slug", "")
                    title = attr.get("title", "")
                    created = attr.get("createdAt") or attr.get("lastPostedAt")
                    topics.append({
                        "id": str(topic_id),
                        "title": title,
                        "url": f"{BASE_BAIPIAO}/bbs/d/{slug}",
                        "created_at": created,
                        "source": f"baipiao_p{page}",
                    })
            except Exception:
                html = fetch(html_url)
                topics = parse_topics(html, BaipiaoHTMLParser)
                for t in topics:
                    t["source"] = f"baipiao_p{page}"
            print(f"[fetch] baipiao page {page}: {len(topics)} topics", file=sys.stderr)
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] baipiao page {page} failed: {e}", file=sys.stderr)
            break
    return all_topics


def fetch_nodeloc() -> list:
    all_topics = []
    for page in range(1, 4):
        api_url = f"{BASE_NODELOC}/latest.json?order=created&page={page}"
        html_url = f"{BASE_NODELOC}/latest?order=created&page={page}"
        try:
            try:
                html = fetch(api_url)
                data = json.loads(html)
                topics = []
                for t in data.get("topic_list", {}).get("topics", []):
                    topic_id = t.get("id")
                    title = t.get("title", "")
                    created = t.get("bumped_at") or t.get("created_at") or t.get("last_posted_at")
                    topics.append({
                        "id": str(topic_id),
                        "title": title,
                        "url": f"{BASE_NODELOC}/t/topic/{topic_id}",
                        "created_at": created,
                        "source": f"nodeloc_p{page}",
                    })
            except Exception:
                html = fetch(html_url)
                topics = parse_topics(html, NodeLocHTMLParser)
                for t in topics:
                    t["source"] = f"nodeloc_p{page}"
            print(f"[fetch] nodeloc page {page}: {len(topics)} topics", file=sys.stderr)
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] nodeloc page {page} failed: {e}", file=sys.stderr)
            break
    return all_topics


def main():
    ap = argparse.ArgumentParser(description="Fetch welfare topics from multiple sources")
    ap.add_argument("--output", "-o", default="data/topics.jsonl")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    all_topics = []
    all_topics.extend(fetch_linuxsb())
    all_topics.extend(fetch_baipiao())
    all_topics.extend(fetch_nodeloc())

    # Deduplicate
    all_topics = deduplicate(all_topics)

    # Score and tag
    all_topics = [score_topic(t) for t in all_topics]

    # Keep only welfare-relevant topics based on title keywords
    def is_relevant(t: dict) -> bool:
        title = t.get("title", "").lower()
        return any(kw.lower() in title for kw in RELEVANCE_KEYWORDS)

    before = len(all_topics)
    all_topics = [t for t in all_topics if is_relevant(t)]
    print(f"[filter] relevance filter: {before} -> {len(all_topics)} topics", file=sys.stderr)

    # Sort by score desc, then by created_at desc, then id desc as tiebreaker
    def sort_key(t):
        score = -t.get("score", 0)
        created = -(int(_parse_sortable(t.get("created_at") or "0")) if t.get("created_at") else 0)
        id_num = -int(re.search(r"\d+", t["id"]).group(0)) if re.search(r"\d+", t["id"]) else 0
        return (score, created, id_num)

    all_topics.sort(key=sort_key)

    # Limit
    all_topics = all_topics[: args.limit]

    # Normalize output
    out = []
    for t in all_topics:
        out.append({
            "id": t["id"],
            "title": t["title"],
            "url": t["url"],
            "tags": t.get("tags", []),
            "score": t.get("score", 0),
            "source": t.get("source", ""),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "created_at": t.get("created_at"),
        })

    with open(args.output, "w", encoding="utf-8") as f:
        for item in out:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"[done] wrote {len(out)} topics to {args.output}", file=sys.stderr)


def _parse_sortable(created_at):
    if not created_at:
        return 0
    try:
        # Handles ISO strings with timezone like 2026-09-28T17:20:50.573Z
        s = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception:
        return 0


if __name__ == "__main__":
    main()
