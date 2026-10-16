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
FEATURED_STATE = Path(__file__).resolve().parent / "data" / "featured.jsonl"
FEATURED_CACHE = Path(__file__).resolve().parent / "data" / "cache" / "content"

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


def render(topics, featured_payload=None, featured_baked_at=0):
    try:
        template = TEMPLATE.read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"template missing: {TEMPLATE} ({e}); cannot render the page")

    html = (
        template
        .replace("__FEATURED_JSON__", _js_payload(featured_payload))
        .replace("__FEATURED_BAKED_AT__", str(int(featured_baked_at)))
        .replace("__CARDS_JSON__", _js_payload(topics))
        .replace("__SOURCE_LABELS_JSON__", json.dumps(SOURCE_LABELS, ensure_ascii=False))
        .replace("__CATEGORY_TONES_JSON__", json.dumps(CATEGORY_TONES, ensure_ascii=False))
        .replace("__CATEGORY_ORDER_JSON__", json.dumps(CATEGORY_ORDER, ensure_ascii=False))
    )
    for token in ("__CARDS_JSON__", "__SOURCE_LABELS_JSON__", "__CATEGORY_TONES_JSON__", "__CATEGORY_ORDER_JSON__",
                  "__FEATURED_JSON__", "__FEATURED_BAKED_AT__"):
        if token in html:
            raise SystemExit(f"template still contains {token} after substitution")
    return html


FEATURED_REPORT = Path(__file__).resolve().parent / "data" / "featured-report.jsonl"


def _append_featured_report(row):
    """精选健康度日报（P3）：每轮一行追加。只在板子有变化或跨天时记，否则一天 48 行重复。"""
    lines = FEATURED_REPORT.read_text(encoding="utf-8").splitlines() if FEATURED_REPORT.exists() else []
    last = json.loads(lines[-1]) if lines else {}
    changed = any(row.get(k, 0) for k in ("added", "updated", "dropped", "pruned"))
    if lines and not changed and last.get("ts", "")[:10] == row["ts"][:10]:
        return                                       # 无变化且当天已记 → 不追加
    with FEATURED_REPORT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_featured(topics):
    """精选板：读 store+正文缓存 → 过门槛 → 合并状态 → 返回 (payload, baked_at)。

    离线、零 token。**任何异常都吞掉**：主站页面不能因为精选板块挂掉而不生成。
    """
    import time
    try:
        import featured
        payload, stats = featured.update(topics, FEATURED_CACHE, FEATURED_STATE)
        print("[featured] {} 项（新增 {added} / 刷新 {updated} / 沿用 {kept} / 下架 {dropped}，其中复核下架 {pruned}）".format(
            len(payload), **stats), file=sys.stderr)
        try:
            report = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.gmtime(time.time() + 8 * 3600)),
                      "board": len(payload), "candidates": stats.get("candidates", 0),
                      "added": stats.get("added", 0), "updated": stats.get("updated", 0),
                      "kept": stats.get("kept", 0), "dropped": stats.get("dropped", 0),
                      "pruned": stats.get("pruned", 0), "by_kind": {}, "reject_top3": {}}
            for it in payload:
                k = (it.get("f") or {}).get("kind") or "?"
                report["by_kind"][k] = report["by_kind"].get(k, 0) + 1
            for why in getattr(featured, "REJECT_REASON", {}).values():
                report["reject_top3"][why] = report["reject_top3"].get(why, 0) + 1
            report["reject_top3"] = dict(sorted(report["reject_top3"].items(), key=lambda kv: -kv[1])[:3])
            _append_featured_report(report)
        except Exception as _re:                       # noqa: BLE001 —— 日报失败不拖停页面
            print(f"[featured-report] 跳过（{_re.__class__.__name__}: {_re}）", file=sys.stderr)
        return payload, time.time()
    except Exception as exc:                      # noqa: BLE001 —— 明确兜住，不能拖停主站
        print(f"[featured] 跳过（{type(exc).__name__}: {exc}）—— 页面照常生成，精选区置空", file=sys.stderr)
        return [], 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", default="data/topics.jsonl")
    ap.add_argument("--output", "-o", default="docs/index.html")
    args = ap.parse_args()

    topics = load_topics(args.input)
    featured_payload, featured_baked_at = build_featured(topics)
    html = render(topics, featured_payload, featured_baked_at)
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
