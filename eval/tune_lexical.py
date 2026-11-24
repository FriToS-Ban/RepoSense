"""M6: tune the lexical channel (BM25F field weights, saturation, query weighting, candidate
depth) on T2-train; never on T2-test.

    python -m eval.tune_lexical                 # -> eval/results/m6-lexical/tuning.json

T2-train (eval/data/t2/train) holds the 100 qualifying commits per repository that precede
T2-test's 50, so every training query is older than every test query.  One pass answers
each training query with every configuration of the grid:

  * the default configuration (System A as of M2);
  * one-factor variations around it (each field weight set to 0 and doubled, k1, b, query
    weights, candidate depth, residual boost, PRF) -> per-factor contributions;
  * a seeded random sample of joint configurations.

The selected configuration maximises mean R@budget (sym_all) on T2-train, ties broken by
nDCG@10.  Its test-set result comes from eval.run_t2 (system A_tuned), compared with A.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

from eval.benchmarks import t2
from eval.metrics import ir_metrics
from eval.run_t2 import iter_queries, ranked_keys

EVAL_DIR = Path(__file__).resolve().parent
SEED = 7
FIELDS = ("name", "qual", "sig", "doc", "body")
DEFAULT_W = {"name": 3.0, "qual": 1.5, "sig": 2.0, "doc": 1.0, "body": 1.0}


def grid(n_random: int = 32) -> dict[str, dict]:
    """name -> RetrievalConfig keyword overrides."""
    g: dict[str, dict] = {"default": {}}
    for f in FIELDS:
        g[f"w_{f}=0"] = {"bm25f_weights": {**DEFAULT_W, f: 0.0}}
        g[f"w_{f}x2"] = {"bm25f_weights": {**DEFAULT_W, f: DEFAULT_W[f] * 2}}
    for k1 in (0.6, 2.0):
        g[f"k1={k1}"] = {"bm25_k1": k1}
    for b in (0.3, 0.9):
        g[f"b={b}"] = {"bm25_b": b}
    for w in (0.0, 0.5, 2.0):
        g[f"pr_text={w}"] = {"pr_text_weight": w}
    for w in (0.0, 0.25, 1.0):
        g[f"context={w}"] = {"context_weight": w}
    for k in (25, 100):
        g[f"lexical_k={k}"] = {"lexical_k": k}
    for m in (0.0, 0.05, 0.3):
        g[f"min_rel={m}"] = {"min_rel_score": m}
    for c in (25, 60):
        g[f"max_cand={c}"] = {"max_candidates": c}
    g["residual=2"] = {"residual_weight": 2.0}
    g["prf=5"] = {"prf_docs": 5}
    g["prf=10,w=.5"] = {"prf_docs": 10, "prf_weight": 0.5}
    rng = random.Random(SEED)
    for i in range(n_random):
        g[f"rand{i:02d}"] = {
            "bm25f_weights": {"name": rng.choice((1.0, 2.0, 3.0, 5.0)), "qual": rng.choice((0.5, 1.5, 3.0)),
                              "sig": rng.choice((1.0, 2.0, 3.0)), "doc": rng.choice((0.0, 0.5, 1.0, 2.0)),
                              "body": rng.choice((0.5, 1.0, 2.0))},
            "bm25_k1": rng.choice((0.8, 1.2, 2.0)), "bm25_b": rng.choice((0.5, 0.75, 0.9)),
            "pr_text_weight": rng.choice((0.5, 1.0, 2.0)), "context_weight": rng.choice((0.25, 0.5, 1.0)),
            "lexical_k": rng.choice((50, 100)), "min_rel_score": rng.choice((0.05, 0.1, 0.15)),
            "max_candidates": rng.choice((40, 60)), "residual_weight": rng.choice((None, 2.0)),
        }
    return g


def run(split: str = "train", configs: dict | None = None, repos=None) -> dict:
    from backend.retrieval.pipeline import RetrievalConfig

    configs = configs or grid()
    cfgs = {name: RetrievalConfig(channels=("lexical",), **kw) for name, kw in configs.items()}
    queries, _meta = t2.load(split)
    per = defaultdict(lambda: defaultdict(list))  # config -> metric -> per-query values
    by_repo = defaultdict(lambda: defaultdict(list))
    t0 = time.perf_counter()
    for n, ctx in enumerate(iter_queries(queries, repos), 1):
        q, retr = ctx.q, ctx.retr
        grades = q.grades("sym_all")
        for name, cfg in cfgs.items():
            res = retr.build_context(ctx.diff, ctx.head, q.message, config=cfg)
            m = ir_metrics(ranked_keys(res.items, retr.snap), grades)
            for k in ("R@all", "nDCG@10", "R@10", "MRR"):
                per[name][k].append(m[k])
            by_repo[name][q.repo].append(m["R@all"])
        if n % 50 == 0:
            print(f"[tune] {n}/{len(queries)} queries, {time.perf_counter() - t0:.0f}s", flush=True)
    table = {name: {k: round(statistics.fmean(v), 4) for k, v in ms.items()} for name, ms in per.items()}
    for name in table:
        table[name]["R@all_by_repo"] = {r: round(statistics.fmean(v), 4) for r, v in sorted(by_repo[name].items())}
    best = max(table, key=lambda c: (table[c]["R@all"], table[c]["nDCG@10"], c == "default"))
    return {"split": split, "queries": len(queries), "configs": configs, "results": table, "selected": best,
            "per_query_R@all": {name: per[name]["R@all"] for name in per}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(EVAL_DIR / "results" / "m6-lexical" / "tuning.json"))
    ap.add_argument("--repos", default="")
    ap.add_argument("--random", type=int, default=32)
    args = ap.parse_args(argv)
    res = run("train", grid(args.random), [r for r in args.repos.split(",") if r] or None)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in res.items() if k != "per_query_R@all"}
    out.write_text(json.dumps(slim, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (out.parent / "tuning_per_query.json").write_text(json.dumps(res["per_query_R@all"], sort_keys=True) + "\n",
                                                      encoding="utf-8")
    d = res["results"]["default"]
    print(f"\n{'config':14s} {'R@all':>7s} {'dR@all':>7s} {'nDCG@10':>8s} {'R@10':>6s}")
    for name, m in sorted(res["results"].items(), key=lambda kv: -kv[1]["R@all"]):
        print(f"{name:14s} {m['R@all']:7.4f} {m['R@all'] - d['R@all']:+7.4f} {m['nDCG@10']:8.4f} {m['R@10']:6.4f}")
    print(f"\nselected: {res['selected']}  {res['configs'][res['selected']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
