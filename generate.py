#!/usr/bin/env python3
"""Generate the single-page site from topics.jsonl (v6 template).

The page shell lives in docs/template.html (design v6). This script only:
  1. loads the store,
  2. embeds the rows as the `const cards=[...]` payload the template's JS renders,
  3. injects the source-label / category-order / category-tone maps from the
     authoritative constants below (the template's own buttons are derived from
     the data at runtime, so a new source or category needs no template edit).

The page stays a pure function of the store: no wall-clock values are baked in.
The "最近收录" badge is rendered client-side from max(fetched_at).
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "docs" / "template.html"

# ---- authoritative display maps (mirror of fetch.py's category authority) ----
# fetch.py owns tagging; generate.py owns display. tests/test_site.py asserts
# every category in fetch.CATEGORY_KEYWORDS appears in ALL of the three tables
# below AND in the rendered page's JS, keeping template and tagger in lockstep.
SOURCE_LABELS = {
    'linuxsb': 'linux.sb',
    'baipiao': 'baipiao.org',
    'nodeloc': 'nodeloc.com',
    'linuxdo': 'linux.do',
    'vibex': 'vibex.iflow.cn',
    'nextbuf': 'nextbuf.com',
}
CATEGORY_ORDER = ['中转站', '公益站', '鸡蛋', '兑换码', '额度', '体验金', '免费放粮', '抽奖', '优惠渠道']
CATEGORY_TONES = {
    '中转站': 'blue', '公益站': 'green', '鸡蛋': 'amber', '兑换码': 'pink',
    '额度': 'purple', '体验金': 'cyan', '免费放粮': 'teal', '抽奖': 'red',
    '优惠渠道': 'orange',
}


def load_topics(path):
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


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


def _write_if_changed(path, text):
    """Write only when the content differs; return whether we wrote.

    The page is a pure function of the store, so on a run with no new posts the
    rendered HTML is byte-identical to what is already on disk. Rewriting it
    anyway just churns a 100+ KB file (and its mtime) for nothing - the
    scheduled job already has the store side of this handled in fetch.py.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == text:
                return False
    except (OSError, UnicodeDecodeError):
        pass
    _atomic_write(path, text)
    return True


def _js_payload(topics):
    """JSON for the embedded <script> payload.

    json.dumps leaves '<' untouched, so a scraped title containing "</script>"
    would break out of the embedding <script> block (stored XSS on the public
    Pages site). Escape the HTML-sensitive characters; the JSON parser on the
    page still yields the original strings.
    """
    return (
        json.dumps(topics, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render(topics):
    try:
        template = TEMPLATE.read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"template missing: {TEMPLATE} ({e}); cannot render the page")

    html = (
        template
        .replace("__CARDS_JSON__", _js_payload(topics))
        .replace("__SOURCE_LABELS_JSON__", json.dumps(SOURCE_LABELS, ensure_ascii=False))
        .replace("__CATEGORY_TONES_JSON__", json.dumps(CATEGORY_TONES, ensure_ascii=False))
        .replace("__CATEGORY_ORDER_JSON__", json.dumps(CATEGORY_ORDER, ensure_ascii=False))
    )
    for token in ("__CARDS_JSON__", "__SOURCE_LABELS_JSON__", "__CATEGORY_TONES_JSON__", "__CATEGORY_ORDER_JSON__"):
        if token in html:
            raise SystemExit(f"template still contains {token} after substitution")
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", default="data/topics.jsonl")
    ap.add_argument("--output", "-o", default="docs/index.html")
    args = ap.parse_args()

    topics = load_topics(args.input)
    html = render(topics)
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    wrote = _write_if_changed(args.output, html)
    print(
        "[done] {} cards -> {}{}".format(
            len(topics), args.output, "" if wrote else " (unchanged, left alone)"
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
