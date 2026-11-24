"""Run the v1 baseline S2 (hot-fixed v1 pipeline, BM25 first stage) on T2.

    python -m eval.run_t2_v1                  # -> eval/results/t2-v1/

Per query the parent snapshot is indexed with the v1 indexer (replayed from git at
V1_COMMIT, fakes for Pinecone/embeddings, as in eval.run) and the real anchor diff is
reviewed by v1 ``get_relevant_context``.  v1 has no PR-text input.

Key mapping (lenient, in v1's favour): v1 names symbols by their bare name, T2 judges
``path::qualname``.  A v1 function item ``path::f`` counts for every symbol of ``path``
whose qualname ends in ``f`` (so ``A.f`` and ``B.f`` both match); a v1 class item also
covers the class's methods.  File items count only in the file view.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from eval import offline
from eval.benchmarks import t2
from eval.metrics import ir_metrics, mean_metrics
from eval.run import PER_QUERY_METRICS, _write_json
from eval.stats import bootstrap_ci

EVAL_DIR = Path(__file__).resolve().parent
CACHE = EVAL_DIR / ".cache"
SYSTEM = "S2"
SEED = 13


def map_items(items, facts) -> list[str]:
    """v1 context items [(key, chars, path)] -> ranked T2 keys (see module docstring)."""
    by_path: dict[str, list] = defaultdict(list)
    for f in facts:
        for s in f.symbols:
            if s.qualname:
                by_path[f.path].append(s)
    ranked = []
    for key, _chars, path in items:
        if "::" not in key:
            ranked.append(path)  # a file chunk: file view only
            continue
        name = key.split("::", 1)[1].split("__chunk")[0]
        matched = [s for s in by_path.get(path, []) if s.qualname.rsplit(".", 1)[-1] == name]
        if not matched:
            ranked.append(f"{path}::{name}")
            continue
        for s in matched:
            ranked.append(f"{path}::{s.qualname}")
            if s.kind == "class":
                ranked += [f"{path}::{c.qualname}" for c in by_path[path] if c.qualname.startswith(s.qualname + ".")]
    return list(dict.fromkeys(ranked))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(EVAL_DIR / "results" / "t2-v1"))
    ap.add_argument("--repos", default="")
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(exist_ok=True)
    db_path = CACHE / f"{out_dir.name}.sqlite"
    if db_path.exists():
        db_path.unlink()
    engine = offline.bootstrap(db_path)
    from sqlalchemy import event

    from backend.core.database import SessionLocal
    from backend.models.models import Repository
    from eval.lexical import BM25
    from eval.run_t2 import iter_queries
    from eval.systems.v1 import V1, V1_COMMIT, V1Bench

    sql = Counter()

    @event.listens_for(engine, "before_cursor_execute")
    def _count(*_a, **_k):
        sql["stmts"] += 1

    db = SessionLocal()
    v1 = V1()
    bench = V1Bench(v1, db, sql)
    bench.stage1.mode = "bm25"
    per, rows, cost = defaultdict(list), {}, defaultdict(list)
    seen_repos: set[str] = set()
    t_start = time.perf_counter()
    with offline.deterministic_uuids(SEED):
        for n, ctx in enumerate(iter_queries(repos=[r for r in args.repos.split(",") if r] or None), 1):
            q = ctx.q
            rid = f"t2-{q.repo}"
            if rid not in seen_repos:
                db.add(Repository(id=rid, repo_name=q.repo, repo_full_name=f"bench/{q.repo}", is_indexed=True,
                                  crawl_permission=True))
                db.commit()
                seen_repos.add(rid)
            offline.STORE.vectors = {k: v for k, v in offline.STORE.vectors.items() if v[1].get("repo_id") != rid}
            files = [{"path": p, "content": t} for p, t in sorted(ctx.contents.items())]
            t0 = time.perf_counter()
            v1.indexer.index_files(rid, files, db, clear_first=True)
            index_s = time.perf_counter() - t0
            bench.stage1.bm25[rid] = BM25(offline.STORE.docs_for_repo(rid))
            items, ms, _returned, c = bench._pipeline(v1.rag_fixed.get_relevant_context, rid, ctx.diff, q.anchor)
            ranked = map_items(items, ctx.facts)
            row = {"qid": q.qid, "corpus": q.repo, "category": "commit", "m": {SYSTEM: {}}}
            for view in t2.VIEWS:
                rl = [k.split("::")[0] for k in ranked] if view == "file" else [k for k in ranked if "::" in k]
                m = ir_metrics(rl, q.grades(view))
                if m is None:
                    continue
                m["lat_ms"] = ms
                per[("commit", SYSTEM, view)].append(m)
                per[(f"{q.repo}:commit", SYSTEM, view)].append(m)
                row["m"][SYSTEM][view] = [round(m[x], 4) for x in PER_QUERY_METRICS]
            rows[q.qid] = row
            cost["commit"].append({"ctx_chars": sum(ch for _, ch, _ in items), "ctx_items": len(items),
                                   "anchor_file_items": sum(p == q.anchor for _, _, p in items),
                                   "sql": c["sql"], "embed": c["embed"], "pinecone": c["pinecone"],
                                   "index_seconds": index_s})
            if n % 25 == 0:
                print(f"[t2-v1] {n} queries, {time.perf_counter() - t_start:.0f}s", flush=True)

    result = {
        "meta": {"benchmark": "t2", "systems": [SYSTEM], "v1_commit": V1_COMMIT, "python": sys.version.split()[0],
                 "key_mapping": "lenient bare-name (see eval/run_t2_v1.py)"},
        "metrics": {f"{c}|{s}|{v}": {**mean_metrics(r), "queries": len(r)} for (c, s, v), r in per.items()},
        "diagnostics": {f"{c}|{SYSTEM}": mean_metrics(r) for c, r in cost.items()},
        "ci95_R@all_sym_all": {}, "ci95_R@all_sym_code": {},
    }
    for (c, s, v), r in per.items():
        if ":" not in c and v in ("sym_all", "sym_code"):
            mean, lo, hi = bootstrap_ci([x["R@all"] for x in r])
            result[f"ci95_R@all_{v}"][f"{c}|{s}"] = [round(mean, 3), round(lo, 3), round(hi, 3)]
    _write_json(out_dir / "metrics.json", result)
    with open(out_dir / "per_query.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for qid in sorted(rows):
            fh.write(json.dumps(rows[qid], sort_keys=True) + "\n")
    for view in t2.VIEWS:
        m = result["metrics"].get(f"commit|{SYSTEM}|{view}")
        if m:
            print(f"{view:10s} " + " ".join(f"{x}={m[x]:.3f}" for x in ("P@5", "R@10", "MRR", "nDCG@10", "R@all", "n")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
