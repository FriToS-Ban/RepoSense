"""Run v2 retrieval systems on T1.

    python -m eval.run_v2                       # all v2 systems
    python -m eval.run_v2 --systems A --out eval/results/t1-v2

Output format matches eval.run (metrics.json + per_query.jsonl), so systems are compared
with ``python -m eval.compare eval/results/t1-v2:A eval/results/t1-v1-baselines:S2_...``.

Coverage semantics for packed contexts (design §6.3):
  * an item's key is ``path::name`` (bare name, the granularity of T1 judgments);
  * a class rendered in full also covers its methods;
  * ``R@all`` counts any rendering; ``R@all_appropriate`` counts a judged *caller* (impact
    queries) only if its call site is shown (callsite/full rendering), because a caller's
    signature alone does not show how it uses the changed function.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from eval import corpora
from eval.benchmarks import t1
from eval.metrics import ir_metrics, mean_metrics
from eval.run import PER_QUERY_METRICS, _write_json
from eval.stats import bootstrap_ci

EVAL_DIR = Path(__file__).resolve().parent


def systems():
    """name -> RetrievalConfig factory.  Add M3+ systems here."""
    from backend.retrieval.pipeline import RetrievalConfig
    from backend.retrieval.presets import TUNED_LEXICAL, production_config

    return {
        "A": lambda: RetrievalConfig(),  # BM25F lexical channel + v2 packing (M2)
        "E": lambda: RetrievalConfig(channels=("structural",)),  # typed 1-hop graph expansion (M3)
        "EA": lambda: RetrievalConfig(channels=("structural", "lexical")),  # RRF fusion of E and A (M3)
        # M5: multi-hop.  *_ppr / *_bfs append PPR / typed-BFS candidates after E's 1-hop list;
        # P is a pure PPR channel (all hops ranked by PPR mass).
        "E_ppr": lambda: RetrievalConfig(channels=("structural",), multihop="ppr"),
        "E_bfs": lambda: RetrievalConfig(channels=("structural",), multihop="bfs"),
        "P": lambda: RetrievalConfig(channels=("ppr",)),
        "EA_ppr": lambda: RetrievalConfig(channels=("structural", "lexical"), multihop="ppr"),
        "EA_bfs": lambda: RetrievalConfig(channels=("structural", "lexical"), multihop="bfs"),
        "PA": lambda: RetrievalConfig(channels=("ppr", "lexical")),
        "A_tuned": lambda: RetrievalConfig(channels=("lexical",), **TUNED_LEXICAL),  # M6, selected on T2-train
        "V2": production_config,  # M11: what production serves
    }


def ltr_systems():
    """M9 learned fusion (models trained on T2-train), evaluated on T1 as a regression check."""
    from backend.retrieval.ltr import LinearRanker
    from backend.retrieval.pipeline import RetrievalConfig
    from backend.retrieval.presets import TUNED_LEXICAL

    out = {}
    for name in ("F", "F_nocc", "F_lexonly"):
        p = EVAL_DIR / "results" / "m9-ltr" / f"model_{name}.json"
        if p.exists():
            m = LinearRanker.load(p)
            out[name] = lambda m=m: RetrievalConfig(channels=tuple(m.meta["generators"]), reranker=m, **TUNED_LEXICAL)
            if name == "F_nocc":
                out["F_nocc_pin"] = lambda m=m: RetrievalConfig(channels=tuple(m.meta["generators"]), reranker=m,
                                                                rerank_pin=("callee_on_changed_line",), **TUNED_LEXICAL)
    return out


def sweep_systems():
    """M5 restart-probability sweep (tuned on the T1 dev half, eval.stats.split_of)."""
    from backend.retrieval.pipeline import RetrievalConfig

    out = {}
    for a in (0.1, 0.2, 0.3, 0.5, 0.7):
        out[f"P_a{a}"] = lambda a=a: RetrievalConfig(channels=("ppr",), ppr_alpha=a)
        out[f"E_ppr_a{a}"] = lambda a=a: RetrievalConfig(channels=("structural",), multihop="ppr", ppr_alpha=a)
    return out


def _bare(qualname: str) -> str:
    return qualname.split(".")[-1] if qualname else ""


def evaluate(names: list[str], out_dir: Path) -> dict:
    from backend.kg.build import build_from_files
    from backend.kg.model import Kind
    from backend.retrieval.pipeline import RepoRetriever

    specs = corpora.manifest()
    files = {s.name: corpora.load_verified(s) for s in specs}
    queries, _ = t1.load()
    factories = {**systems(), **sweep_systems(), **ltr_systems()}
    per_sys = defaultdict(list)
    diag = defaultdict(list)
    failure = defaultdict(Counter)
    rows = {q.qid: {"qid": q.qid, "corpus": q.corpus, "category": q.category, "m": {}} for q in queries}
    index_stats = {}
    for corpus in [s.name for s in specs]:
        content = {f["path"]: f["content"] for f in files[corpus]}
        t0 = time.perf_counter()
        graph = build_from_files(corpus, files[corpus])
        t_graph = time.perf_counter() - t0
        for name in names:
            retr = RepoRetriever(graph, factories[name]())
            index_stats[f"{corpus}|{name}"] = {"graph_seconds": round(t_graph, 2), "index_seconds": round(retr.build_seconds, 2)}
            snap = retr.snap
            all_keys = {f"{s.path}::{_bare(s.qualname)}" for s in snap.symbols if s.kind != Kind.MODULE}
            for q in (q for q in queries if q.corpus == corpus):
                lines = content[q.path].split("\n")
                diff = t1.make_diff(q.path, lines, q.ws, q.we, q.category)
                res = retr.build_context(diff, content.get)
                ranked, appropriate = [], set()
                for it in res.items:
                    if it.index is None:
                        continue
                    s = snap.symbols[it.index]
                    key = f"{s.path}::{_bare(s.qualname)}" if s.qualname else s.path
                    ranked.append(key)
                    if it.rendering in ("callsite", "full", "seed"):
                        appropriate.add(key)
                    if it.rendering == "full" and s.kind == Kind.CLASS:
                        for c in snap.children(it.index):
                            ck = f"{snap.symbols[c].path}::{_bare(snap.symbols[c].qualname)}"
                            ranked.append(ck)
                            appropriate.add(ck)
                lat = res.trace["timings_ms"]["total_ms"]
                for view in t1.VIEWS[q.category]:
                    rl = [k.split("::")[0] for k in ranked] if view == "file_xfile" else ranked
                    g = q.grades(view)
                    m = ir_metrics(rl, g)
                    if m is None:
                        continue
                    if view != "file_xfile":
                        hits_appr = sum(1 for k in g if k in appropriate) if q.category == "impact" else sum(1 for k in g if k in set(ranked))
                        m["R@all_appropriate"] = hits_appr / len(g)
                    m["lat_ms"] = lat
                    per_sys[(q.category, name, view)].append(m)
                    per_sys[(f"{q.corpus}:{q.category}", name, view)].append(m)
                    rows[q.qid]["m"].setdefault(name, {})[view] = [round(m[x], 4) for x in PER_QUERY_METRICS]
                tr = res.trace
                renders = Counter(p["rendering"] for p in tr["packed"])
                # noise accounting: how many packed items each channel contributed, and how many are judged relevant
                judged = set(q.grades("sym_all")) | (set(q.grades("2hop_only")) if q.category == "deps" else set())
                noise = Counter()
                for it in res.items:
                    if it.rendering == "seed" or it.index is None:
                        continue
                    s = snap.symbols[it.index]
                    rel = f"{s.path}::{_bare(s.qualname)}" in judged
                    for ch in {c.split(":")[0] for c in it.channel.split("+")}:
                        noise[f"packed_{ch}"] += 1
                        noise[f"packed_relevant_{ch}"] += int(rel)
                seed_chars = sum(p["chars"] for p in tr["packed"] if p["rendering"] == "seed")
                diag[(q.category, name)].append({
                    "ctx_chars": tr["chars"], "ctx_items": len(tr["packed"]),
                    "share_of_context_from_seeds": seed_chars / max(tr["chars"], 1),
                    "candidates": len(tr["candidates"]), "structural_change": 1.0 if tr["structural_change"] else 0.0,
                    **{f"items_{r}": renders.get(r, 0) for r in ("seed", "full", "compact", "callsite")},
                    "link_ms": tr["timings_ms"]["link_ms"], "candidates_ms": tr["timings_ms"]["candidates_ms"],
                    "pack_ms": tr["timings_ms"]["pack_ms"],
                    **{k: noise.get(k, 0) for k in ("packed_lexical", "packed_relevant_lexical",
                                                    "packed_structural", "packed_relevant_structural")},
                })
                cand_keys = {c["key"].split("::")[0] + "::" + _bare(c["key"].split("::")[1]) for c in tr["candidates"]}
                main = {d for d in q.main_gt() if not d.startswith(q.path + "::")}
                for d in main:
                    if d not in all_keys:
                        failure[(q.category, name)]["not_indexed_as_symbol"] += 1
                    elif d in ranked and (q.category != "impact" or d in appropriate):
                        failure[(q.category, name)]["in_context"] += 1
                    elif d in ranked:
                        failure[(q.category, name)]["in_context_wrong_rendering"] += 1
                    elif d in cand_keys:
                        failure[(q.category, name)]["generated_then_packed_out"] += 1
                    else:
                        failure[(q.category, name)]["not_generated"] += 1
        print(f"[v2] {corpus}: graph {t_graph:.2f}s", flush=True)
    result = {
        "meta": {"benchmark": "t1", "t1_version": t1.T1_VERSION, "systems": names, "python": sys.version.split()[0]},
        "index": index_stats,
        "metrics": {f"{c}|{s}|{v}": {**mean_metrics(r), "queries": len(r)} for (c, s, v), r in per_sys.items()},
        "diagnostics": {f"{c}|{s}": mean_metrics(r) for (c, s), r in diag.items()},
        "failure_breakdown_xfile_gt": {f"{c}|{s}": dict(sorted(v.items())) for (c, s), v in failure.items()},
        "ci95_R@all_sym_xfile": {}, "latency_ms": {},
    }
    for (c, s, v), r in per_sys.items():
        if ":" in c:
            continue
        if v == "sym_xfile":
            mean, lo, hi = bootstrap_ci([x["R@all"] for x in r])
            result["ci95_R@all_sym_xfile"][f"{c}|{s}"] = [round(mean, 3), round(lo, 3), round(hi, 3)]
        if v == "sym_all":
            ls = sorted(x["lat_ms"] for x in r)
            result["latency_ms"][f"{c}|{s}"] = {"p50": round(ls[len(ls) // 2], 2), "p95": round(ls[int(len(ls) * 0.95) - 1], 2)}
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "metrics.json", result)
    with open(out_dir / "per_query.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for row in rows.values():
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--systems", default="all")
    ap.add_argument("--out", default=str(EVAL_DIR / "results" / "t1-v2"))
    args = ap.parse_args(argv)
    names = {"all": list(systems()), "sweep": list(sweep_systems()), "ltr": list(ltr_systems())}.get(args.systems) \
        or args.systems.split(",")
    res = evaluate(names, Path(args.out))
    cols = ("R@10", "R@20", "MRR", "nDCG@10", "R@all", "R@all_appropriate", "n")
    for cat in ("deps", "impact"):
        print(f"\n{cat} / sym_xfile")
        for key, m in sorted(res["metrics"].items()):
            c, s, v = key.split("|")
            if c == cat and v == "sym_xfile":
                print(f"  {s:10s}" + "".join(f" {x}={m.get(x, float('nan')):.3f}" for x in cols) + f"  q={m['queries']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
