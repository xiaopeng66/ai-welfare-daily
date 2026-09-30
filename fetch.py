#!/usr/bin/env python3
"""
linuxsb-daily fetcher
Scrapes multiple sites for AI 中转站福利 posts and emits a JSON lines file.
Sources:
  - linux.sb: /forum/2, /forum/8, /index.php?sort=lucky, /index.php?sort=card, /
  - baipiao.org: /bbs/api/discussions
  - nodeloc.com: /latest.json, /c/welfare/12.json
"""
import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
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

# Per-run counter of failed source fetches; used to refuse writing a store that
# silently lost a whole source (e.g. CI without a working proxy).
FETCH_ERRORS = []


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


def fetch_topic_published_time(topic_id: str) -> str | None:
    """Fetch a topic page and extract the true published time from meta tags."""
    url = f"{BASE_LINUXSB}/topic/{topic_id}"
    try:
        html = fetch(url, timeout=15)
        match = re.search(
            r'<meta[^>]+property="article:published_time"[^>]+content="([^"]+)"',
            html,
        )
        if match:
            published = match.group(1)
            dt = datetime.fromisoformat(published)
            return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    return None


CATEGORY_KEYWORDS = {
    "中转站": ["中转站"],
    "公益站": ["公益站", "公益", "免费使用", "零门槛", "免费"],
    "鸡蛋": ["鸡蛋"],
    "兑换码": ["兑换码", "邀请码", "注册码", "code", "key", "cdk"],
    "额度": ["额度", "刀", "credit", "送"],
    "体验金": ["体验金", "积分"],
    "抽奖": ["抽奖", "盲盒", "中奖", "欧皇"],
}

# Strong relevance keywords; title must contain at least one
RELEVANCE_KEYWORDS = [
    "中转站", "公益站", "鸡蛋", "兑换码", "额度", "体验金", "抽奖",
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
        m = re.search(r'/bbs/d/(\d+)', href)
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


def fetch_linuxsb(known_ids: set | None = None) -> list:
    """Fetch linux.sb listings.

    known_ids: ids already stored with a trustworthy created_at. Detail pages
    are only fetched for ids NOT in this set, keeping incremental runs cheap.
    """
    known_ids = known_ids or set()
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
                    "_ts_fallback": created_at,
                })
            for t in topics:
                t["source"] = name
            print(f"[fetch] {name}: {len(topics)} topics", file=sys.stderr)
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] {name} failed: {e}", file=sys.stderr)
            FETCH_ERRORS.append(f"linux.sb listing {name}: {e}")

    # data-performance-time is the last-modified time, not the post creation time.
    # Fetch detail pages only for topics we will actually keep: skip ids already
    # stored, and skip titles the relevance filter would drop anyway.
    def _relevant(t):
        title = t.get("title", "").lower()
        return any(kw.lower() in title for kw in RELEVANCE_KEYWORDS)

    need_detail = [
        t["id"] for t in all_topics
        if t["id"] not in known_ids and _relevant(t)
    ]
    skipped = len(all_topics) - len(need_detail)
    print(
        f"[fetch] linux.sb detail pages: {len(need_detail)} to fetch, "
        f"{skipped} skipped (cached or irrelevant)",
        file=sys.stderr,
    )

    published_map = {}
    if need_detail:
        with ThreadPoolExecutor(max_workers=6) as executor:
            future_to_id = {
                executor.submit(fetch_topic_published_time, tid): tid
                for tid in need_detail
            }
            for future in as_completed(future_to_id):
                tid = future_to_id[future]
                try:
                    published_map[tid] = future.result()
                except Exception:
                    pass

    # Drop topics we already have; they will be merged back from the store.
    all_topics = [t for t in all_topics if t["id"] not in known_ids]

    for t in all_topics:
        true_time = published_map.get(t["id"])
        if true_time:
            t["created_at"] = true_time
            t["published_verified"] = True
        elif not t.get("created_at"):
            t["created_at"] = t.pop("_ts_fallback", None)
        else:
            t.pop("_ts_fallback", None)

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
                        # baipiao's API createdAt IS the publish time.
                        "published_verified": True,
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
            FETCH_ERRORS.append(f"baipiao page {page}: {e}")
            break
    return all_topics


def fetch_nodeloc_welfare() -> list:
    """Fetch nodeloc.com welfare category (抽奖福利)."""
    all_topics = []
    for page in range(1, 4):
        api_url = f"{BASE_NODELOC}/c/welfare/12.json?order=created&page={page}"
        try:
            html = fetch(api_url)
            data = json.loads(html)
            topics = []
            for t in data.get("topic_list", {}).get("topics", []):
                topic_id = t.get("id")
                title = t.get("title", "")
                created = t.get("created_at") or t.get("bumped_at") or t.get("last_posted_at")
                topics.append({
                    "id": str(topic_id),
                    "title": title,
                    "url": f"{BASE_NODELOC}/t/topic/{topic_id}",
                    "created_at": created,
                    "source": f"nodeloc_welfare_p{page}",
                    "published_verified": True,
                })
            print(f"[fetch] nodeloc welfare page {page}: {len(topics)} topics", file=sys.stderr)
            all_topics.extend(topics)
        except Exception as e:
            print(f"[warn] nodeloc welfare page {page} failed: {e}", file=sys.stderr)
            FETCH_ERRORS.append(f"nodeloc welfare page {page}: {e}")
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
                    # Prefer created_at; bumped_at/last_posted_at are reply times
                    created = t.get("created_at") or t.get("bumped_at") or t.get("last_posted_at")
                    topics.append({
                        "id": str(topic_id),
                        "title": title,
                        "url": f"{BASE_NODELOC}/t/topic/{topic_id}",
                        "created_at": created,
                        "source": f"nodeloc_p{page}",
                        # nodeloc's API created_at IS the publish time.
                        "published_verified": True,
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
            FETCH_ERRORS.append(f"nodeloc page {page}: {e}")
            break
    return all_topics


def main():
    ap = argparse.ArgumentParser(description="Fetch welfare topics from multiple sources")
    ap.add_argument("--output", "-o", default="data/topics.jsonl")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    # Load existing topics for incremental merge
    existing = {}
    if os.path.exists(args.output):
        try:
            with open(args.output, "r", encoding="utf-8") as f:
                for line in f:
                    t = json.loads(line)
                    existing[t["id"]] = t
            print(f"[merge] loaded {len(existing)} existing topics", file=sys.stderr)
        except Exception:
            pass

    # linux.sb ids whose created_at is trustworthy (real publish time, not the
    # last-modified time from the listing). Only these may skip detail fetches.
    # Scoped to linuxsb_* sources: other sites use their own id namespaces.
    known_linuxsb_ids = {
        t["id"]
        for t in existing.values()
        if t.get("published_verified")
        and str(t.get("source", "")).startswith("linuxsb_")
    }

    seen_ids_path = os.path.join(os.path.dirname(args.output) or ".", "seen_ids.txt")
    seen_ids = set()
    if os.path.exists(seen_ids_path):
        try:
            with open(seen_ids_path, "r", encoding="utf-8") as f:
                seen_ids = {line.strip() for line in f if line.strip()}
        except Exception:
            pass

    linuxsb_topics = fetch_linuxsb(known_linuxsb_ids)
    baipiao_topics = fetch_baipiao()
    nodeloc_topics = fetch_nodeloc()
    nodeloc_welfare_topics = fetch_nodeloc_welfare()
    all_topics = linuxsb_topics + baipiao_topics + nodeloc_topics + nodeloc_welfare_topics

    # Partial failures are survivable now that we merge incrementally: cached
    # rows for the failed source stay in the store. Warn loudly (the workflow
    # turns this into a red run) but still write.
    if FETCH_ERRORS:
        print(f"[warn] {len(FETCH_ERRORS)} fetch error(s); cached rows are kept:", file=sys.stderr)
        for err in FETCH_ERRORS:
            print(f"        - {err}", file=sys.stderr)
        # Marker so CI can surface a partial run as a red build *after* the
        # good data has been committed and pushed.
        status_path = os.path.join(os.path.dirname(args.output) or ".", ".fetch_errors")
        try:
            with open(status_path, "w", encoding="utf-8") as f:
                f.write("\n".join(FETCH_ERRORS) + "\n")
        except Exception:
            pass
    else:
        status_path = os.path.join(os.path.dirname(args.output) or ".", ".fetch_errors")
        if os.path.exists(status_path):
            os.remove(status_path)

    # Total blackout: nothing fetched at all AND we already have data. That is a
    # network/proxy outage, not an empty site. Refuse to write rather than risk
    # clobbering the store.
    if not all_topics and existing:
        print(
            "[fatal] every source returned 0 topics while the store has "
            f"{len(existing)} rows — refusing to write (network/proxy outage?)",
            file=sys.stderr,
        )
        sys.exit(2)

    # Deduplicate fetched topics
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

    # Merge fetched topics with existing: new data overrides old for same id
    merged = dict(existing)
    collisions = []
    for t in all_topics:
        prev = merged.get(t["id"])
        if prev is not None and prev.get("url") and t.get("url") and prev["url"] != t["url"]:
            collisions.append((t["id"], prev.get("url"), t.get("url")))
        merged[t["id"]] = t
    if collisions:
        for cid, old_url, new_url in collisions:
            print(f"[warn] id collision {cid}: {old_url} -> {new_url}", file=sys.stderr)
    print(f"[merge] {len(existing)} existing + {len(all_topics)} fetched -> {len(merged)} total", file=sys.stderr)

    # Cap total: drop the OLDEST posts first (rolling window). Missing
    # created_at counts as oldest so the cap always holds.
    def age_key(t):
        ts = _parse_sortable(t.get("created_at"))
        return ts if ts else 0

    merged_list = list(merged.values())
    if len(merged_list) > args.limit:
        merged_list.sort(key=age_key, reverse=True)  # newest first
        dropped = merged_list[args.limit:]
        merged_list = merged_list[: args.limit]
        print(
            f"[limit] trimmed {len(dropped)} oldest posts (cap {args.limit}); "
            f"oldest dropped id={dropped[0].get('id')} at {dropped[0].get('created_at')}",
            file=sys.stderr,
        )

    # Display order: score desc, then created_at desc, then id desc
    def sort_key(t):
        score = -t.get("score", 0)
        created = -age_key(t)
        id_num = -int(re.search(r"\d+", t["id"]).group(0)) if re.search(r"\d+", t["id"]) else 0
        return (score, created, id_num)

    merged_list.sort(key=sort_key)

    # Normalize output
    out = []
    for t in merged_list:
        out.append({
            "id": t["id"],
            "title": t["title"],
            "url": t["url"],
            "tags": t.get("tags", []),
            "score": t.get("score", 0),
            "source": t.get("source", ""),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "created_at": t.get("created_at"),
            "published_verified": bool(t.get("published_verified")),
            "is_new": t["id"] not in seen_ids,
        })

    with open(args.output, "w", encoding="utf-8") as f:
        for item in out:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    # Persist seen ids for next "new vs old" splitting
    all_ids = {t["id"] for t in merged_list}
    merged_seen = seen_ids | all_ids
    with open(seen_ids_path, "w", encoding="utf-8") as f:
        for id_ in sorted(merged_seen):
            f.write(id_ + "\n")

    print(f"[done] wrote {len(out)} topics to {args.output}", file=sys.stderr)


def _parse_sortable(created_at):
    if not created_at:
        return 0
    try:
        s = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception:
        return 0


if __name__ == "__main__":
    main()
