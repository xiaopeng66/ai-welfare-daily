"""冻结精选规则的对拍语料：topics + 正文缓存 + 当板状态 → tests/evidence/featured-<日期>/。

产物：
  topics.jsonl   data/topics.jsonl 全量
  cache.zip      data/cache/content/ 打包（368 个正文 JSON，zip 传输省 368 个文件）
  featured.jsonl data/featured.jsonl（当板状态）
  manifest.json  冻结时刻 + 各文件 md5（ab 工具校验用）

用法：python3 tools/freeze_featured_corpus.py [--out tests/evidence/featured-YYYYMMDD]
只读数据、写 evidence 目录，不碰任何生成物。
"""
import argparse
import datetime
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    now = datetime.datetime.now(featured_cst())
    out = Path(a.out) if a.out else ROOT / "tests" / "evidence" / f"featured-{now:%Y%m%d}"
    if out.exists():
        raise SystemExit(f"已存在：{out}（换 --out 或先删除）")
    out.mkdir(parents=True)

    topics = ROOT / "data" / "topics.jsonl"
    state = ROOT / "data" / "featured.jsonl"
    cache = ROOT / "data" / "cache" / "content"
    if not cache.is_dir():
        raise SystemExit(f"正文缓存不存在：{cache}")

    shutil.copy2(topics, out / "topics.jsonl")
    shutil.copy2(state, out / "featured.jsonl")
    with zipfile.ZipFile(out / "cache.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(cache.glob("*.json")):
            z.write(f, f.name)

    manifest = {
        "frozen_at": now.isoformat(),
        "topics_md5": md5(out / "topics.jsonl"),
        "featured_md5": md5(out / "featured.jsonl"),
        "cache_files": len(list(cache.glob("*.json"))),
        "cache_md5": md5(out / "cache.zip"),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"冻结完成：{out}")
    print(f"  冻结时刻 {manifest['frozen_at']}")
    print(f"  topics {manifest['topics_md5'][:8]}  featured {manifest['featured_md5'][:8]}  "
          f"cache {manifest['cache_files']} 文件 {manifest['cache_md5'][:8]}")


def featured_cst():
    """与 featured.py 同一时区定义（避免 import featured 引入规则代码——语料冻结必须与规则版本无关）。"""
    from datetime import timezone, timedelta
    return timezone(timedelta(hours=8))


if __name__ == "__main__":
    main()
