"""M9: train the linear reranker on T2-train; choose L2 by leave-one-repo-out CV; never touch T2-test.

    python -m eval.train_ltr                       # -> eval/results/m9-ltr/{model_*.json, cv.json}

1. Collect: for every T2-train query (600 commits, all older than T2-test), the union of the
   lexical, structural (1-hop), PPR and co-change candidates with their features
   (backend/retrieval/ltr.py) and labels (judged = changed by the same commit).  Cached in
   eval/.cache/ltr/train_rows.jsonl.
2. For each variant and each L2 in L2_GRID: leave-one-repo-out CV.  Train on five
   repositories (negatives subsampled, seeded), rerank the held-out repository's pools, and
   score recall of the top 40 (what packing receives) and nDCG@10.  The L2 with the best
   mean held-out R@40 wins.
3. Refit on all six repositories with that L2 and save the model.

Variants (generators = channels whose candidates enter the pool; features = groups used):
  F          all generators, all features                    (full learned fusion)
  F_nograph  all generators, no graph features               (graph only as a generator)
  F_nocc     no co-change candidates or features             (deployable without a history index)
  F_lexpool  lexical candidates only, all features            (graph only as a ranking signal)
  F_lexonly  lexical candidates only, lexical + node features (a learned lexical reranker)
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path

from eval.benchmarks import t2
from eval.run_t2 import iter_queries

EVAL_DIR = Path(__file__).resolve().parent
CACHE = EVAL_DIR / ".cache" / "ltr"
OUT = EVAL_DIR / "results" / "m9-ltr"
L2_GRID = (0.1, 1.0, 10.0, 100.0)
NEG_PER_POS = 20
TOP = 40


def variants():
    from backend.retrieval.ltr import FEATURES, GROUPS

    nograph = tuple(f for f in FEATURES if f not in GROUPS["graph"])
    nocc = tuple(f for f in FEATURES if f not in GROUPS["cochange"])
    lexnode = tuple(f for f in FEATURES if f in GROUPS["lexical"] or f in GROUPS["node"])
    allgen = ("lexical", "structural", "ppr", "cochange")
    return {
        "F": (allgen, FEATURES),
        "F_nograph": (allgen, nograph),
        "F_nocc": (("lexical", "structural", "ppr"), nocc),  # deployable without a history index (M11)
        "F_lexpool": (("lexical",), FEATURES),
        "F_lexonly": (("lexical",), lexnode),
    }


def base_config():
    """The lexical configuration the features are computed with (M6's selection, presets.TUNED_LEXICAL)."""
    from backend.retrieval.pipeline import RetrievalConfig
    from backend.retrieval.presets import TUNED_LEXICAL

    return RetrievalConfig(**TUNED_LEXICAL)


def collect(split: str = "train", repos=None) -> list[dict]:
    from backend.retrieval.diff import parse_unified_diff
    from backend.retrieval.ltr import candidate_features
    from backend.retrieval.seeds import link

    cfg = base_config()
    queries, _ = t2.load(split)
    out, t0 = [], time.perf_counter()
    for n, ctx in enumerate(iter_queries(queries, repos), 1):
        q, retr = ctx.q, ctx.retr
        change = link(retr.snap, parse_unified_diff(ctx.diff), ctx.head)
        overlay = retr._overlay_out(change, ctx.head)
        cc = ctx.cochange
        rows = candidate_features(retr, change, q.message, cfg, overlay, cc.related,
                                  lambda a, b, cc=cc: cc.pair.get(a, {}).get(b, 0))
        judged = set(q.grades("sym_all"))
        snap = retr.snap
        cands = []
        for i, f, lab in rows:
            key = f"{snap.symbols[i].path}::{snap.symbols[i].qualname}"
            cands.append({"f": {k: round(v, 6) for k, v in f.items() if v}, "y": int(key in judged), "g": lab})
        out.append({"qid": q.qid, "repo": q.repo, "judged": len(judged), "cands": cands})
        if n % 100 == 0:
            print(f"[ltr] collected {n}/{len(queries)} ({time.perf_counter() - t0:.0f}s)", flush=True)
    return out


def _gens(label: str) -> set[str]:
    from backend.retrieval.ltr import _generator

    return {_generator(x) for x in label.split("+")}


def pool(qrow: dict, generators) -> list[dict]:
    g = set(generators)
    return [c for c in qrow["cands"] if _gens(c["g"]) & g]


def evaluate_ranker(model, queries: list[dict], generators) -> dict:
    r_top, ndcg = [], []
    for qrow in queries:
        cands = sorted(pool(qrow, generators), key=lambda c: -model.score(c["f"]))
        rel = [c["y"] for c in cands]
        r_top.append(sum(rel[:TOP]) / max(1, qrow["judged"]))
        dcg = sum(r / math.log2(k + 2) for k, r in enumerate(rel[:10]))
        ideal = sum(1 / math.log2(k + 2) for k in range(min(10, qrow["judged"])))
        ndcg.append(dcg / ideal if ideal else 0.0)
    return {"R@40": statistics.fmean(r_top), "nDCG@10": statistics.fmean(ndcg)}


def train(queries: list[dict], generators, features, l2: float):
    from backend.retrieval.ltr import fit

    rows = [(c["f"], c["y"]) for qrow in queries for c in pool(qrow, generators)]
    return fit(rows, features, l2=l2, neg_per_pos=NEG_PER_POS, seed=0)


_DATA: list[dict] = []


def _load(path: str) -> None:
    global _DATA
    _DATA = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


def _cv_task(task):
    name, l2, held = task
    gens, feats = variants()[name]
    m = train([r for r in _DATA if r["repo"] != held], gens, feats, l2)
    return name, l2, held, evaluate_ranker(m, [r for r in _DATA if r["repo"] == held], gens)


def main(argv=None) -> int:
    from concurrent.futures import ProcessPoolExecutor

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recollect", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / "train_rows.jsonl"
    if args.recollect or not cache.exists():
        data = collect()
        cache.write_text("\n".join(json.dumps(r, sort_keys=True) for r in data) + "\n", encoding="utf-8")
    _load(str(cache))
    data = _DATA
    repos = sorted({r["repo"] for r in data})
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"queries": len(data), "repos": repos, "l2_grid": list(L2_GRID), "neg_per_pos": NEG_PER_POS,
              "pool_recall": {}, "variants": {}}
    tasks = [(name, l2, held) for name in variants() for l2 in L2_GRID for held in repos]
    results = defaultdict(lambda: defaultdict(list))
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_load, initargs=(str(cache),)) as ex:
        for name, l2, _held, ev in ex.map(_cv_task, tasks):
            for k, v in ev.items():
                results[(name, l2)][k].append(v)
    print(f"[ltr] cross-validation: {len(tasks)} fits in {time.perf_counter() - t0:.0f}s", flush=True)
    for name, (gens, feats) in variants().items():
        judged = sum(r["judged"] for r in data)
        report["pool_recall"][name] = round(sum(c["y"] for r in data for c in pool(r, gens)) / judged, 4)
        cv = {str(l2): {k: round(statistics.fmean(v), 4) for k, v in results[(name, l2)].items()} for l2 in L2_GRID}
        for l2 in L2_GRID:
            print(f"[ltr] {name} l2={l2}: {cv[str(l2)]}", flush=True)
        best = max(L2_GRID, key=lambda l2: (cv[str(l2)]["R@40"], cv[str(l2)]["nDCG@10"]))
        model = train(data, gens, feats, best)
        model.meta.update({"variant": name, "generators": list(gens), "l2_selected": best, "cv": cv,
                           "trained_on": "T2-train (600 queries, 6 repos)"})
        (OUT / f"model_{name}.json").write_text(model.to_json() + "\n", encoding="utf-8")
        report["variants"][name] = {"l2": best, "cv": cv, "generators": list(gens),
                                    "weights": {f: round(w, 4) for f, w in zip(model.features, model.weights)}}
    (OUT / "cv.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "variants"}, indent=1))
    for name, v in report["variants"].items():
        print(name, "l2", v["l2"], v["cv"][str(v["l2"])])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
