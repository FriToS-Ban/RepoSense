"""Run a benchmark.

    python -m eval.run                      # T1, v1 baselines S0–S4, scaling experiment
    python -m eval.run --no-scaling --out eval/results/dev
    python -m eval.run --regenerate         # rebuild eval/data/t1 from the manifest corpora

Outputs (``--out``, default eval/results/t1-v1-baselines):
    metrics.json     aggregates per (category, system, view), per corpus, CIs, latency,
                     diagnostics, failure breakdown, index cost, KG edge quality, scaling
    per_query.jsonl  per query x system x view: [R@10, R@all, nDCG@10, MRR] (input to eval.compare)
and TREC run files in eval/.cache/runs/<out-name>/ (for trec_eval cross-checks).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from eval import corpora, offline
from eval.benchmarks import t1
from eval.metrics import ir_metrics, mean_metrics
from eval.stats import bootstrap_ci

EVAL_DIR = Path(__file__).resolve().parent
CACHE = EVAL_DIR / ".cache"
PER_QUERY_METRICS = ("R@10", "R@all", "nDCG@10", "MRR")
CHUNKER_COMMITS = ["7ce9e6d", "37403a2", "fc23fb5", "4887694", "563bbeb"]
SCALING_PARTS = ("starlette", "fastapi", "httpx", "pygithub", "pydantic")


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _load_queries(specs, files, args):
    meta_now = {s.name: {"pin": s.pin, "fingerprint": corpora.fingerprint(files[s.name])} for s in specs}
    if args.regenerate or not (t1.DATA_DIR / "queries.jsonl").exists():
        queries = t1.generate([(s.name, files[s.name]) for s in specs], seed=args.seed)
        t1.save(queries, {"seed": args.seed, "corpora": meta_now})
        print(f"[t1] generated {len(queries)} queries -> {t1.DATA_DIR}")
        return queries
    queries, meta = t1.load()
    if meta["corpora"] != meta_now and not args.allow_corpus_drift:
        sys.exit("T1 data was generated from different corpora; use --regenerate or --allow-corpus-drift")
    return queries


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(EVAL_DIR / "results" / "t1-v1-baselines"))
    ap.add_argument("--seed", type=int, default=t1.DEFAULT_SEED)
    ap.add_argument("--regenerate", action="store_true")
    ap.add_argument("--no-scaling", action="store_true")
    ap.add_argument("--allow-corpus-drift", action="store_true")
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
    from eval.reference import RefGraph
    from eval.systems.v1 import SYSTEMS, V1, V1Bench, key_path
    from eval.systems.v1_kg import kg_stats

    sql = Counter()

    @event.listens_for(engine, "before_cursor_execute")
    def _count(*_a, **_k):
        sql["stmts"] += 1

    specs = corpora.manifest()
    files = {s.name: corpora.load_verified(s, strict=not args.allow_corpus_drift) for s in specs}
    queries = _load_queries(specs, files, args)
    db = SessionLocal()
    v1 = V1()
    bench = V1Bench(v1, db, sql)
    result: dict = {
        "meta": {"benchmark": "t1", "t1_version": t1.T1_VERSION, "seed": args.seed, "v1_commit": v1_commit(),
                 "python": sys.version.split()[0], "queries": len(queries)},
        "index": {}, "kg": {},
    }

    uuid_ctx = offline.deterministic_uuids(args.seed)
    uuid_ctx.__enter__()
    try:
        for s in specs:
            result["index"][s.name] = bench.index(s.name, files[s.name], f"repo-{s.name}")
            print(f"[index] {s.name}: {result['index'][s.name]['seconds_local_cpu']}s", flush=True)
        for s in specs:
            result["kg"][s.name] = kg_stats(db, f"repo-{s.name}", files[s.name], RefGraph(files[s.name]))

        # S0 sanity: the shipped v1 function swallows an UnboundLocalError and returns ""
        offline.LOG_RECORDS.clear()
        probe = t1.make_diff("httpx/_client.py", ["def f():", "    return 1"], 1, 2, "deps")
        ctx = v1.rag_shipped.get_relevant_context("repo-httpx", probe, db, changed_file_paths=["httpx/_client.py"])
        result["s0_as_shipped"] = {"returned_context_chars": len(ctx), "log": [r for r in offline.LOG_RECORDS if r[1] == "ERROR"]}

        result["diff_chunker_on_real_commits"] = []
        for sha in CHUNKER_COMMITS:
            d = subprocess.run(["git", "-C", str(offline.REPO_ROOT), "show", "--format=", sha], capture_output=True).stdout.decode("utf-8", "ignore")
            ch = v1.rag_fixed.extract_diff_chunks(d)
            result["diff_chunker_on_real_commits"].append({
                "sha": sha, "files": d.count("diff --git"), "chunks": len(ch),
                "header_only_chunks": sum(1 for _, c in ch if "@@" not in c), "chunks_beyond_cap_8": max(0, len(ch) - 8)})

        per_sys = defaultdict(list)
        diag = defaultdict(list)
        failure = defaultdict(Counter)
        per_query = []
        runs = defaultdict(list)
        lines_cache = {name: {f["path"]: f["content"].split("\n") for f in fs} for name, fs in files.items()}
        for q in queries:
            rid = f"repo-{q.corpus}"
            out = bench.run_query(q, rid, lines_cache[q.corpus][q.path])
            row = {"qid": q.qid, "corpus": q.corpus, "category": q.category, "m": {}}
            for sys_name in SYSTEMS:
                ranked = out["ranked"][sys_name]
                for rank, k in enumerate(dict.fromkeys(ranked), 1):
                    runs[sys_name].append(f"{q.qid} Q0 {k.replace(' ', '_')} {rank} {1.0 / rank:.6f} {sys_name}")
                row["m"][sys_name] = {}
                for view in t1.VIEWS[q.category]:
                    rl = [key_path(k) for k in ranked] if view == "file_xfile" else ranked
                    m = ir_metrics(rl, q.grades(view))
                    if m is None:
                        continue
                    m["lat_ms"] = out["latency_ms"][sys_name]
                    per_sys[(q.category, sys_name, view)].append(m)
                    per_sys[(f"{q.corpus}:{q.category}", sys_name, view)].append(m)
                    row["m"][sys_name][view] = [round(m[x], 4) for x in PER_QUERY_METRICS]
            per_query.append(row)
            _diagnose(q, out, bench, diag, failure)

        result["metrics"] = {f"{c}|{s}|{v}": {**mean_metrics(rows), "queries": len(rows)} for (c, s, v), rows in per_sys.items()}
        result["latency_ms"] = {}
        result["ci95_R@all_sym_xfile"] = {}
        for (c, s, v), rows in per_sys.items():
            if ":" in c:
                continue
            if v == "sym_all":
                ls = sorted(r["lat_ms"] for r in rows)
                result["latency_ms"][f"{c}|{s}"] = {"p50": round(ls[len(ls) // 2], 2), "p95": round(ls[int(len(ls) * 0.95) - 1], 2)}
            if v == "sym_xfile":
                mean, lo, hi = bootstrap_ci([r["R@all"] for r in rows])
                result["ci95_R@all_sym_xfile"][f"{c}|{s}"] = [round(mean, 3), round(lo, 3), round(hi, 3)]
        result["diagnostics"] = {f"{c}|{t}": mean_metrics(rows) for (c, t), rows in diag.items()}
        result["failure_breakdown_xfile_gt"] = {f"{c}|{t}": dict(sorted(v.items())) for (c, t), v in failure.items()}

        if not args.no_scaling:
            result.update(_scaling(bench, db, files, args.seed))
    finally:
        uuid_ctx.__exit__(None, None, None)

    _write_json(out_dir / "metrics.json", result)
    with open(out_dir / "per_query.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for row in per_query:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    run_dir = CACHE / "runs" / out_dir.name
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    for sys_name, lines in runs.items():
        (run_dir / f"{sys_name}.run").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _print_summary(result)
    print(f"\nwrote {out_dir / 'metrics.json'}, {out_dir / 'per_query.jsonl'}, runs in {run_dir}")
    return 0


def v1_commit() -> str:
    from eval.systems.v1 import V1_COMMIT

    return V1_COMMIT


def _diagnose(q, out, bench, diag, failure):
    from eval.systems.v1 import key_path

    main_gt = set(q.main_gt())
    xg = {k for k in main_gt if not k.startswith(q.path + "::")}
    k2n = bench.key2nodes[f"repo-{q.corpus}"]
    for sys_name, tag in (("S1_fixed_pipeline+oracle_stage1", "S1"), ("S2_fixed_pipeline+bm25_stage1", "S2")):
        items = out["context"][sys_name]
        tot = sum(sz for _, sz, _ in items) or 1
        in_ctx = {k for k, _, _ in items}
        ret_keys = {bench.meta[n][0] for n in out["returned"][sys_name]}
        graph_added = [k for k in in_ctx if k not in ret_keys and key_path(k) != q.path]
        cost = out["cost"][sys_name]
        diag[(q.category, tag)].append({
            "ctx_chars": tot,
            "share_of_context_from_changed_file": sum(sz for _, sz, p in items if p == q.path) / tot,
            "ctx_nodes": len(items),
            "budget_exhausted": 1.0 if tot >= 11500 else 0.0,
            "graph_added_nodes": len(graph_added),
            "graph_added_relevant": sum(1 for k in graph_added if k in main_gt),
            "sql_stmts": cost["sql"], "embed_calls": cost["embed"], "pinecone_queries": cost["pinecone"],
        })
        for k in sorted(xg):
            if k not in k2n:
                failure[(q.category, tag)]["not_indexed_as_node"] += 1
            elif k in in_ctx:
                failure[(q.category, tag)]["in_context"] += 1
            elif k in ret_keys:
                failure[(q.category, tag)]["retrieved_then_dropped(dedup/budget)"] += 1
            else:
                failure[(q.category, tag)]["not_retrieved_by_stage1"] += 1
    top5 = out["ranked"]["S3_bm25_raw"][:5]  # raw node hits (the slots a top-5 vector query would fill)
    diag[(q.category, "S3")].append({
        "top5_from_changed_file": sum(1 for k in top5 if key_path(k) == q.path) / max(len(top5), 1),
        "top1_is_query_function_itself": 1.0 if top5 and top5[0] == f"{q.path}::{q.name}" else 0.0,
    })


def _scaling(bench, db, files, seed):
    """BM25 quality on the same deps queries when the repo is 5.5x larger (audit §6.5)."""
    import random

    from eval.reference import RefGraph
    from eval.systems.v1_kg import kg_stats

    mono = [f for part in SCALING_PARTS for f in files[part]]
    out = {"index_monorepo": bench.index("monorepo", mono, "repo-mono")}
    out["kg_monorepo"] = kg_stats(db, "repo-mono", mono, RefGraph(mono))
    rng = random.Random(seed)
    scale = {}
    for name in ("starlette", "httpx"):
        rg = RefGraph(files[name])
        lines = {f["path"]: f["content"].split("\n") for f in files[name]}
        deps, _ = t1.sample(rg, rng)
        for rid, label in ((f"repo-{name}", "alone"), ("repo-mono", "monorepo")):
            rows = []
            for sid, ws, we, one, _two in deps:
                path = sid[0]
                diff = t1.make_diff(path, lines[path], ws, we, "deps")
                hunk = bench.v1.rag_fixed.extract_diff_chunks(diff)[-1][1]
                ranked = [bench.meta[h][0] for h, _ in bench.stage1.bm25[rid].search(hunk, 50)]
                rows.append(ir_metrics(ranked, {t1.sym_key(*k): 2 for k in one if k[0] != path}))
            scale[f"{name}@{label}"] = mean_metrics(rows)
    out["scaling_bm25_sym_xfile"] = scale
    return out


def _print_summary(result):
    cols = ("P@5", "R@5", "R@10", "R@20", "MRR", "nDCG@10", "R@all")
    for cat in ("deps", "impact"):
        print(f"\n{cat} / sym_xfile")
        print(f"  {'system':34s}" + "".join(f"{c:>8s}" for c in cols) + "  queries")
        for key, m in sorted(result["metrics"].items()):
            c, s, v = key.split("|")
            if c == cat and v == "sym_xfile":
                print(f"  {s:34s}" + "".join(f"{m[x]:8.3f}" for x in cols) + f"  {m['queries']}")


if __name__ == "__main__":
    t0 = time.perf_counter()
    rc = main()
    print(f"elapsed {time.perf_counter() - t0:.1f}s")
    sys.exit(rc)
