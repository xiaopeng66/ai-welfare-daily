"""Offline A/B for gate (is_relevant) and tag (score_topic) rule changes.

Why this exists: a rule edit can only be judged against the posts the gate
**rejected** as well as the ones it kept — "did we stop missing freebies" and
"did we start admitting noise" are two different diffs, and both live in the
pre-filter corpus. Fetch results alone cannot show either.

Usage:
    # 1. freeze a corpus (pre-filter = before the relevance gate)
    LINUXDO_ENABLED=0 python3 fetch.py -o /tmp/store.jsonl \
        --dump-candidates /tmp/candidates.jsonl --probe-missing 0

    # 2. compare working tree against any revision
    python3 tests/ab_rules.py --corpus /tmp/candidates.jsonl --base HEAD

    # 3. also show what the new rules would do to the live store
    python3 tests/ab_rules.py --corpus /tmp/candidates.jsonl --base HEAD \
        --store data/topics.jsonl

No network or input writes. Writes a temporary baseline module and optional
--jsonl report. Rule differences do not fail the command; input/output errors do.
Malformed JSONL rows are reported and skipped. Judgement stays with the human.
"""

import argparse
import collections
import copy
import importlib.util
import json
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_base(rev):
    src = subprocess.run(
        ["git", "show", f"{rev}:fetch.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    fd, path = tempfile.mkstemp(suffix=".py", prefix=f"fetch_{rev.replace('/', '_')}_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(src)
    return load_module(path, "fetch_base"), path


def read_rows(path):
    rows = []
    with open(path, encoding="utf-8") as source:
        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get("title", ""), (str, type(None))):
                    raise ValueError("expected an object with a string title")
            except ValueError:
                print(f"[report] skip invalid JSONL line {line_no}: {path}")
                continue
            rows.append(row)
    return rows


def unique_titles(corpus_path):
    seen, titles = set(), []
    for row in read_rows(corpus_path):
        title = (row.get("title") or "").strip()
        if title and title not in seen:
            seen.add(title)
            titles.append(title)
    return titles


def tag_counts(mod, titles):
    counts = collections.Counter()
    untagged = []
    for title in titles:
        tags = mod.score_topic({"title": title})["tags"]
        if not tags:
            untagged.append(title)
        for tag in tags:
            counts[tag] += 1
    return counts, untagged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True,
                    help="pre-filter corpus from fetch.py --dump-candidates")
    ap.add_argument("--base", default="HEAD", help="git revision of fetch.py to compare against")
    ap.add_argument("--store", default="", help="optional store jsonl to evaluate the new rules on")
    ap.add_argument("--show", type=int, default=40, help="max titles to list per diff")
    ap.add_argument("--jsonl", default="", help="write one base/new verdict per input candidate and store row")
    args = ap.parse_args()

    base_mod, base_path = load_base(args.base)
    new_mod = load_module(os.path.join(ROOT, "fetch.py"), "fetch_new")
    titles = unique_titles(args.corpus)

    base_keep = [t for t in titles if base_mod.is_relevant_title(t)]
    new_keep = [t for t in titles if new_mod.is_relevant_title(t)]
    added = [t for t in new_keep if t not in set(base_keep)]
    removed = [t for t in base_keep if t not in set(new_keep)]

    print(f"[gate] {len(titles)} unique titles: base {len(base_keep)} -> new {len(new_keep)} "
          f"(+{len(added)} / -{len(removed)})")
    for label, items in (("+", added), ("-", removed)):
        for title in items[: args.show]:
            print(f"   {label} {title}")
        if len(items) > args.show:
            print(f"   {label} ... {len(items) - args.show} more")

    common = [t for t in new_keep if t in set(base_keep)]
    base_counts, base_untagged = tag_counts(base_mod, common)
    new_counts, new_untagged = tag_counts(new_mod, common)
    print(f"\n[tags] {len(common)} titles kept by both: untagged {len(base_untagged)} "
          f"-> {len(new_untagged)}")
    for tag in sorted(set(base_counts) | set(new_counts)):
        if base_counts[tag] != new_counts[tag]:
            print(f"   {tag}: {base_counts[tag]} -> {new_counts[tag]}")
    for title in new_untagged[: args.show]:
        print(f"   still untagged: {title}")

    changed = [t for t in common
               if base_mod.score_topic({"title": t})["tags"] != new_mod.score_topic({"title": t})["tags"]]
    print(f"\n[tags] {len(changed)} title(s) changed tags:")
    for title in changed[: args.show]:
        b = ",".join(base_mod.score_topic({"title": title})["tags"])
        n = ",".join(new_mod.score_topic({"title": title})["tags"])
        print(f"   [{b or '-'} -> {n or '-'}] {title}")

    rows = []
    if args.store and os.path.exists(args.store):
        rows = read_rows(args.store)
        purge = [r for r in rows if not new_mod.is_relevant_title(r.get("title") or "")]
        retag = []
        for row in rows:
            # Purged rows are not relabelled by the pipeline. Snapshot BEFORE
            # calling a scorer which may mutate both the dict and its tag list.
            if not new_mod.is_relevant_title(row.get("title") or ""):
                continue
            before = (list(row.get("tags") or []), int(row.get("score") or 0))
            fresh = new_mod.score_topic(copy.deepcopy(row))
            after = (list(fresh.get("tags") or []), int(fresh.get("score") or 0))
            if before != after:
                retag.append((row, before, after))
        print(f"\n[store] {len(rows)} row(s): new gate would drop {len(purge)}, "
              f"re-tag would change {len(retag)} (retained rows only)")
        for r in purge[: args.show]:
            print(f"   purge | {r.get('title', '')} | {r.get('url', '')}")
        for row, before, after in retag[: args.show]:
            print(f"   retag | {before} -> {after} | {row.get('title', '')} | {row.get('url', '')}")

    if args.jsonl:
        candidate_rows = read_rows(args.corpus)
        with open(args.jsonl, "w", encoding="utf-8") as report:
            for row in candidate_rows:
                title = row.get("title") or ""
                old_kept = base_mod.is_relevant_title(title)
                new_kept = new_mod.is_relevant_title(title)
                old = base_mod.score_topic({"title": title}) if old_kept else {"tags": [], "score": 0}
                new = new_mod.score_topic({"title": title}) if new_kept else {"tags": [], "score": 0}
                report.write(json.dumps({
                    "kind": "candidate", "url": row.get("url", ""),
                    "source": row.get("source", ""), "title": title,
                    "base_keep": old_kept, "new_keep": new_kept,
                    "base_tags": old["tags"], "new_tags": new["tags"],
                    "base_score": old["score"], "new_score": new["score"],
                }, ensure_ascii=False) + "\n")
            if args.store and os.path.exists(args.store):
                for row in rows:
                    title = row.get("title") or ""
                    base_kept = base_mod.is_relevant_title(title)
                    new_kept = new_mod.is_relevant_title(title)
                    base = base_mod.score_topic(copy.deepcopy(row)) if base_kept else {"tags": [], "score": 0}
                    new = new_mod.score_topic(copy.deepcopy(row)) if new_kept else {"tags": [], "score": 0}
                    report.write(json.dumps({
                        "kind": "store", "url": row.get("url", ""),
                        "source": row.get("source", ""), "title": title,
                        "base_keep": base_kept, "new_keep": new_kept,
                        "base_tags": base.get("tags") or [], "new_tags": new.get("tags") or [],
                        "base_score": base.get("score") or 0, "new_score": new.get("score") or 0,
                        "stored_tags": row.get("tags") or [], "stored_score": row.get("score") or 0,
                    }, ensure_ascii=False) + "\n")
        print(f"[report] candidate and store verdicts -> {args.jsonl}")

    os.unlink(base_path)


if __name__ == "__main__":
    main()
