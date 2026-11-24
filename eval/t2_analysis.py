"""Diagnostics for T2 (M4): why do graph channels under-perform lexical retrieval on history?

    python -m eval.t2_analysis                 # -> eval/results/t2-v2/analysis.json + printed tables

Per judged symbol (ground truth = symbols the same commit changed in other files):

  * graph distance from the seeds over the *union* of the base graph and the overlay graph
    (anchor file re-resolved at HEAD) — the best case for any graph method.  Edges are
    treated as undirected; relation types: CALLS/REFERENCES (dep), OVERRIDES, INHERITS,
    TESTS, CONTAINS.  ``relation_missing`` (design §6.4) = no path of length <= 2;
  * the relation pattern of one shortest path (e.g. ``caller``, ``caller>caller``);
  * its rank in each channel's own candidate list (lexical, structural, cochange), and in a
    deep lexical search (k=1000 per seed, no score floor) — is it retrievable at all?
  * its fused rank in EA (before the max_candidates cut).

Per query: RRF displacement — relevant items in A's top 10 that are missing from EA's top
10, and what took their place.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict, deque
from pathlib import Path

from eval.benchmarks import t2
from eval.run_t2 import iter_queries, ranked_keys, systems

EVAL_DIR = Path(__file__).resolve().parent
MAX_DIST = 4


def _rel(etype: str, forward: bool) -> str:
    """Name of the relation from the *seed side* walking one edge (forward = along src->dst)."""
    if etype in ("CALLS", "REFERENCES"):
        return "callee" if forward else "caller"
    if etype == "OVERRIDES":
        return "overridden" if forward else "override"
    if etype == "INHERITS":
        return "base" if forward else "subclass"
    if etype == "TESTS":
        return "tested" if forward else "tests"
    return "parent" if forward else "child"  # CONTAINS


def union_adjacency(graphs) -> dict[str, list[tuple[str, str]]]:
    adj: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for g in graphs:
        for e in g.edges.values():
            if e.type == "IMPORTS":
                continue  # module-level; every file importing a package would be within 2 hops
            adj[e.src].add((e.dst, _rel(e.type, True)))
            adj[e.dst].add((e.src, _rel(e.type, False)))
        for s in g.symbols.values():
            if s.parent_id and s.kind != "module" and g.symbols.get(s.parent_id) and g.symbols[s.parent_id].kind != "module":
                adj[s.id].add((s.parent_id, "parent"))
                adj[s.parent_id].add((s.id, "child"))
    return {k: sorted(v) for k, v in adj.items()}


def bfs(adj, sources: list[str], max_dist: int = MAX_DIST) -> dict[str, tuple[int, str]]:
    """node -> (distance, relation pattern of the first shortest path found)."""
    out = {s: (0, "") for s in sources}
    dq = deque(sorted(sources))
    while dq:
        u = dq.popleft()
        d, pat = out[u]
        if d >= max_dist:
            continue
        for v, rel in adj.get(u, ()):
            if v not in out:
                out[v] = (d + 1, f"{pat}>{rel}" if pat else rel)
                dq.append(v)
    return out


def analyse(repos=None) -> dict:
    from backend.kg.build import build_graph
    from backend.kg.extract_python import extract
    from backend.kg.model import Kind, symbol_id
    from backend.retrieval.diff import parse_unified_diff
    from backend.retrieval.lexical import query_from_lines
    from backend.retrieval.seeds import link
    from backend.retrieval.structural import expand, rrf

    cfgs = systems()
    judged_rows, query_rows = [], []
    for ctx in iter_queries(repos=repos):
        q, retr = ctx.q, ctx.retr
        snap, cfg = retr.snap, cfgs["EA"]
        change = link(snap, parse_unified_diff(ctx.diff), ctx.head)
        exclude = {i for p in change.changed_paths for i in snap.symbols_in(p)}
        exclude |= {sd.index for sd in change.seeds if sd.index is not None}

        # --- graph distance over base ∪ overlay
        facts = {f.path: f for f in ctx.facts}
        facts[q.anchor] = extract(q.anchor, ctx.head_anchor)
        overlay = build_graph(q.repo, list(facts.values()))
        adj = union_adjacency([ctx.graph, overlay])
        seed_ids = [symbol_id(q.repo, sd.path, sd.qualname) for sd in change.seeds]
        dist = bfs(adj, seed_ids)
        anchor_ids = [s.id for s in overlay.symbols.values() if s.path == q.anchor] + \
                     [s.id for s in ctx.graph.symbols.values() if s.path == q.anchor]
        dist_file = bfs(adj, sorted(set(anchor_ids)))

        # --- per-channel candidate lists (as the pipeline produces them)
        lex = retr._lexical_candidates(change.seeds, exclude, q.message, cfg)
        ov = retr._overlay_out(change, ctx.head) if cfg.overlay else None
        struct = expand(snap, change.seeds, cfg.structural_limit, use_plan=cfg.use_plan, overlay_out=ov)
        coch = retr._cochange_candidates(change, exclude, q.message, ctx.cochange.related, cfg)
        fused = rrf({"structural": struct, "lexical": lex}, cfg.rrf_k, cfg.channel_weights)
        deep = {}
        for sd in change.seeds:
            qq = query_from_lines(sd.query_lines, q.message, cfg.pr_text_weight)
            for r, (i, _s) in enumerate(retr.lexical.search(qq, 1000, exclude) if qq else [], 1):
                deep[i] = min(deep.get(i, 10 ** 6), r)

        def ranks(lst):
            return {f"{snap.symbols[i].path}::{snap.symbols[i].qualname}": r for r, (i, _s, _l) in enumerate(lst, 1)}

        r_lex, r_struct, r_coch, r_fused = ranks(lex), ranks(struct), ranks(coch), ranks(fused)
        r_deep = {f"{snap.symbols[i].path}::{snap.symbols[i].qualname}": r for i, r in deep.items()}

        ctx_a = retr.build_context(ctx.diff, ctx.head, q.message, config=cfgs["A"])
        ctx_ea = retr.build_context(ctx.diff, ctx.head, q.message, config=cfgs["EA"])
        ctx_e = retr.build_context(ctx.diff, ctx.head, q.message, config=cfgs["E"])
        ranked_a, ranked_ea, ranked_e = (ranked_keys(c.items, snap) for c in (ctx_a, ctx_ea, ctx_e))
        judged = q.grades("sym_all")
        for j in q.judgments:
            sid = symbol_id(q.repo, *j.key.split("::", 1))
            d, pat = dist.get(sid, (None, ""))
            judged_rows.append({
                "qid": q.qid, "repo": q.repo, "key": j.key, "test": j.test,
                "in_base": snap.index.get(sid) is not None,
                "dist": d, "pattern": pat, "dist_from_anchor_file": dist_file.get(sid, (None, ""))[0],
                "rank_lexical": r_lex.get(j.key), "rank_lexical_deep": r_deep.get(j.key),
                "rank_structural": r_struct.get(j.key), "rank_cochange": r_coch.get(j.key),
                "rank_fused_EA": r_fused.get(j.key),
                "in_ctx_A": j.key in ranked_a, "in_ctx_EA": j.key in ranked_ea, "in_ctx_E": j.key in ranked_e,
            })

        # --- RRF displacement (A top-10 vs EA top-10)
        top_a, top_ea = ranked_a[:10], ranked_ea[:10]
        lost = [k for k in top_a if k in judged and k not in top_ea]
        label = {it.key: it.channel for it in ctx_ea.items}
        intruders = [k for k in top_ea if k not in top_a]
        struct_items_e = [it for it in ctx_e.items if it.index is not None and it.rendering != "seed"]
        query_rows.append({
            "qid": q.qid, "repo": q.repo, "seeds": len(change.seeds),
            "seeds_added": sum(sd.index is None for sd in change.seeds),
            "lost_from_top10": len(lost),
            "intruders_top10": len(intruders),
            "intruders_structural_only": sum("lexical" not in label.get(k, "lexical") for k in intruders),
            "intruders_relevant": sum(k in judged for k in intruders),
            "E_packed": len(struct_items_e),
            "E_packed_anchor_file": sum(snap.symbols[it.index].path == q.anchor for it in struct_items_e),
            "E_packed_test_files": sum(bool(t2.TEST_RE.search(snap.symbols[it.index].path)) for it in struct_items_e),
            "struct_candidates": len(struct), "lex_candidates": len(lex),
        })
        if len(query_rows) % 50 == 0:
            print(f"[t2-analysis] {len(query_rows)} queries", flush=True)
    return summarise(judged_rows, query_rows)


def _share(rows, pred) -> float:
    return round(sum(1 for r in rows if pred(r)) / len(rows), 3) if rows else float("nan")


def summarise(judged_rows: list[dict], query_rows: list[dict]) -> dict:
    out = {"judged": len(judged_rows), "queries": len(query_rows), "by_group": {}}
    groups = {"all": judged_rows, "code": [r for r in judged_rows if not r["test"]],
              "tests": [r for r in judged_rows if r["test"]]}
    for g, rows in groups.items():
        dist_hist = Counter("unreachable" if r["dist"] is None else str(r["dist"]) for r in rows)
        hop1 = Counter(r["pattern"] for r in rows if r["dist"] == 1)
        hop2 = Counter(r["pattern"] for r in rows if r["dist"] == 2)
        out["by_group"][g] = {
            "n": len(rows),
            "in_base_snapshot": _share(rows, lambda r: r["in_base"]),
            "graph_distance": {k: round(v / len(rows), 3) for k, v in sorted(dist_hist.items())},
            "relation_missing_le2": _share(rows, lambda r: r["dist"] is None or r["dist"] > 2),
            "within_2_of_anchor_file": _share(rows, lambda r: r["dist_from_anchor_file"] is not None
                                              and r["dist_from_anchor_file"] <= 2),
            "hop1_relations": dict(hop1.most_common()),
            "hop2_patterns_top": dict(hop2.most_common(8)),
            "generated_lexical": _share(rows, lambda r: r["rank_lexical"] is not None),
            "generated_lexical_deep_k1000": _share(rows, lambda r: r["rank_lexical_deep"] is not None),
            "generated_structural": _share(rows, lambda r: r["rank_structural"] is not None),
            "generated_cochange": _share(rows, lambda r: r["rank_cochange"] is not None),
            "generated_any_channel": _share(rows, lambda r: any(r[k] is not None for k in
                                                                ("rank_lexical", "rank_structural", "rank_cochange"))),
            "generated_structural_only": _share(rows, lambda r: r["rank_structural"] is not None and r["rank_lexical"] is None),
            "generated_cochange_only": _share(rows, lambda r: r["rank_cochange"] is not None and r["rank_lexical"] is None
                                              and r["rank_structural"] is None),
            "fused_EA_rank_gt_40": _share([r for r in rows if r["rank_fused_EA"] is not None],
                                          lambda r: r["rank_fused_EA"] > 40),
            "lexical_rank_gt_40": _share([r for r in rows if r["rank_lexical"] is not None], lambda r: r["rank_lexical"] > 40),
            "in_ctx_A": _share(rows, lambda r: r["in_ctx_A"]),
            "in_ctx_E": _share(rows, lambda r: r["in_ctx_E"]),
            "in_ctx_EA": _share(rows, lambda r: r["in_ctx_EA"]),
            "in_ctx_E_given_dist1": _share([r for r in rows if r["dist"] == 1], lambda r: r["in_ctx_E"]),
            "in_ctx_A_given_dist1": _share([r for r in rows if r["dist"] == 1], lambda r: r["in_ctx_A"]),
            "in_ctx_A_given_unreachable": _share([r for r in rows if r["dist"] is None or r["dist"] > 2],
                                                 lambda r: r["in_ctx_A"]),
        }
    qr = query_rows
    out["queries_summary"] = {
        "mean_seeds": round(statistics.mean(r["seeds"] for r in qr), 2),
        "share_queries_with_added_seed": _share(qr, lambda r: r["seeds_added"] > 0),
        "mean_lost_from_A_top10_in_EA": round(statistics.mean(r["lost_from_top10"] for r in qr), 3),
        "share_queries_losing_relevant_top10": _share(qr, lambda r: r["lost_from_top10"] > 0),
        "mean_EA_top10_intruders": round(statistics.mean(r["intruders_top10"] for r in qr), 2),
        "mean_EA_top10_intruders_structural_only": round(statistics.mean(r["intruders_structural_only"] for r in qr), 2),
        "mean_EA_top10_intruders_relevant": round(statistics.mean(r["intruders_relevant"] for r in qr), 3),
        "mean_E_packed": round(statistics.mean(r["E_packed"] for r in qr), 2),
        "share_E_packed_in_anchor_file": round(sum(r["E_packed_anchor_file"] for r in qr) /
                                               max(1, sum(r["E_packed"] for r in qr)), 3),
        "share_E_packed_in_test_files": round(sum(r["E_packed_test_files"] for r in qr) /
                                              max(1, sum(r["E_packed"] for r in qr)), 3),
        "mean_struct_candidates": round(statistics.mean(r["struct_candidates"] for r in qr), 2),
        "mean_lex_candidates": round(statistics.mean(r["lex_candidates"] for r in qr), 2),
    }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(EVAL_DIR / "results" / "t2-v2" / "analysis.json"))
    ap.add_argument("--repos", default="")
    args = ap.parse_args(argv)
    res = analyse([r for r in args.repos.split(",") if r] or None)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(res, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
