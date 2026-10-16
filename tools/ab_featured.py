"""精选规则 A/B 对拍：基线版本的 featured.py vs 工作区版本，跑同一份冻结语料，输出四张差异表。

用法：
  python3 tools/ab_featured.py --evidence tests/evidence/featured-20261017 [--base HEAD]

四张表：
  ① 新增   基线拒 / 当前过
  ② 下架   基线过 / 当前拒
  ③ 字段变化 kind / amount / level / level_class / reqs / score
  ④ 摘要变化 lines 逐行对照

只报告、不写数据、退出码恒 0（判断仍由人做，与 tests/ab_rules.py 同一哲学）。
基线规则从 `git show <base>:featured.py` 加载——比的是"那个提交真正会跑的代码"。
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIELDS = ("kind", "amount", "level", "level_class", "reqs", "score")


def git_show(path: str, ref: str) -> str:
    git = sys.executable and "git"
    try:
        r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True,
                           check=True, cwd=str(ROOT))
        return r.stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        for cand in (r"C:\Program Files\Git\cmd\git.exe", "/usr/bin/git"):
            try:
                r = subprocess.run([cand, "show", f"{ref}:{path}"], capture_output=True, text=True,
                                   check=True, cwd=str(ROOT))
                return r.stdout
            except Exception:
                continue
        raise SystemExit(f"git show 失败：{e}")


def load_module(src: str, tag: str):
    ns = {"__name__": f"featured@{tag}"}
    exec(compile(src, f"<{tag}>", "exec"), ns)
    return ns


def run_build(build, rows, cache, now, tag):
    try:
        items = build(rows, cache, now)
        return {it["key"]: it for it in items}, None
    except Exception as e:  # noqa: BLE001 —— 对拍工具要能报告"这个版本跑不起来"
        return {}, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--base", default="HEAD")
    a = ap.parse_args()

    ev = Path(a.evidence)
    manifest = json.loads((ev / "manifest.json").read_text(encoding="utf-8"))
    now = datetime.datetime.fromisoformat(manifest["frozen_at"])

    rows = [json.loads(l) for l in (ev / "topics.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(ev / "cache.zip") as z:
            z.extractall(td)
        import featured as cur_mod            # 工作区版本
        cache = cur_mod.load_cache(td)

        base_src = git_show("featured.py", a.base)
        base_ns = load_module(base_src, a.base)

        base_items, base_err = run_build(base_ns["build_items"], rows, cache, now, "base")
        cur_items, cur_err = run_build(cur_mod.build_items, rows, cache, now, "worktree")

    print(f"语料 {ev.name}（冻结 {manifest['frozen_at'][:16]}）  基线 {a.base}")
    print(f"基线过板 {len(base_items)} 条  工作区过板 {len(cur_items)} 条"
          + (f"  ⚠ 基线跑挂：{base_err}" if base_err else "")
          + (f"  ⚠ 工作区跑挂：{cur_err}" if cur_err else ""))
    if base_err or cur_err:
        return

    bk, ck = set(base_items), set(cur_items)

    def label(it):
        f = it["f"]
        return f"[{f['kind']:7}] {f.get('amount','')[:12]:12} {it['key'][:30]:30} {(f.get('lines') or [''])[0][:34]}"

    added = sorted(ck - bk)
    removed = sorted(bk - ck)
    print(f"\n=== ① 新增（基线拒 / 当前过）：{len(added)} ===")
    for k in added:
        print("  +", label(cur_items[k]))
    print(f"\n=== ② 下架（基线过 / 当前拒）：{len(removed)} ===")
    for k in removed:
        print("  -", label(base_items[k]))

    changed = [k for k in bk & ck if any(base_items[k].get(x) != cur_items[k].get(x) for x in FIELDS)]
    print(f"\n=== ③ 字段变化：{len(changed)} ===")
    for k in sorted(changed):
        b, c = base_items[k], cur_items[k]
        diffs = [x for x in FIELDS if b.get(x) != c.get(x)]
        print(f"  {k[:36]:36}")
        for x in diffs:
            print(f"     {x}: {b.get(x)!s:.44} → {c.get(x)!s:.44}")

    sum_changed = [k for k in bk & ck
                   if (base_items[k].get("lines") or []) != (cur_items[k].get("lines") or [])]
    print(f"\n=== ④ 摘要变化：{len(sum_changed)} ===")
    for k in sum_changed:
        b, c = base_items[k].get("lines") or [], cur_items[k].get("lines") or []
        print(f"  {k[:36]:36}")
        for i in range(max(len(b), len(c))):
            ob = b[i] if i < len(b) else "(无)"
            nc = c[i] if i < len(c) else "(无)"
            if ob != nc:
                print(f"     行{i+1}: {ob[:52]}\n            → {nc[:52]}")

    if not (added or removed or changed or sum_changed):
        print("\n零差异。")
    print("\n（工具只报差异，判断由人做；退出码恒 0。）")


if __name__ == "__main__":
    main()
