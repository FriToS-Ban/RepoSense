"""T3 context sufficiency for the v1 baseline S2 (hot-fixed v1 pipeline, BM25 first stage).

    python -m eval.run_t3_v1                  # -> eval/results/t3/v1.json

Each repository is indexed once at its pin with the replayed v1 indexer; every case's
mutation diff is then reviewed by v1 ``get_relevant_context``.  v1 items are whole function
or class chunks, so a needed symbol counts as present when a v1 item of the same file has its
bare name (lenient, in v1's favour, as in eval/run_t2_v1.py).
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from eval import offline

EVAL_DIR = Path(__file__).resolve().parent


def main() -> int:
    cache = EVAL_DIR / ".cache"
    cache.mkdir(exist_ok=True)
    db_path = cache / "t3-v1.sqlite"
    if db_path.exists():
        db_path.unlink()
    offline.bootstrap(db_path)
    from backend.core.database import SessionLocal
    from backend.models.models import Repository
    from eval.benchmarks import t2, t3
    from eval.lexical import BM25
    from eval.run_t2 import Snapshotter
    from eval.stats import bootstrap_ci
    from eval.systems.v1 import V1, V1Bench

    db = SessionLocal()
    v1 = V1()
    bench = V1Bench(v1, db, Counter())
    bench.stage1.mode = "bm25"
    cases = t3.load()
    per = defaultdict(list)
    anchor_share = []
    with offline.deterministic_uuids(13):
        for spec in t2.manifest():
            rc = [c for c in cases if c.repo == spec["name"]]
            if not rc:
                continue
            sn = Snapshotter(t2.ensure_clone(spec))
            _facts, contents = sn.facts_at(spec["pin"])
            rid = f"t3-{spec['name']}"
            db.add(Repository(id=rid, repo_name=spec["name"], repo_full_name=f"bench/{spec['name']}", is_indexed=True,
                              crawl_permission=True))
            db.commit()
            v1.indexer.index_files(rid, [{"path": p, "content": t} for p, t in sorted(contents.items())], db, clear_first=True)
            bench.stage1.bm25[rid] = BM25(offline.STORE.docs_for_repo(rid))
            for c in rc:
                diff, _head = t3.make_diff(c, contents[c.path])
                items, _ms, _ret, _cost = bench._pipeline(v1.rag_fixed.get_relevant_context, rid, diff, c.path)
                have = {(p, k.split("::", 1)[1].split("__chunk")[0]) for k, _ch, p in items if "::" in k}
                ok = any((k.split("::")[0], k.split("::")[1].rsplit(".", 1)[-1]) in have for k in c.needed)
                per[c.mutation].append(int(ok))
                per["all"].append(int(ok))
                anchor_share.append(sum(ch for _k, ch, p in items if p == c.path) / max(1, sum(ch for _k, ch, _p in items)))
            print(f"[t3-v1] {spec['name']}: {len(rc)} cases", flush=True)
    out = {}
    for k, v in per.items():
        mean, lo, hi = bootstrap_ci(v)
        out[k] = {"rate": round(mean, 4), "ci95": [round(lo, 4), round(hi, 4)], "n": len(v)}
    res = {"system": "S2_fixed_pipeline+bm25_stage1", "sufficiency": out,
           "share_of_context_from_mutated_file": round(sum(anchor_share) / len(anchor_share), 4)}
    (EVAL_DIR / "results" / "t3").mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "results" / "t3" / "v1.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(res, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
