"""Structural candidate channel: typed 1-hop expansion from seeds (design §5.3).

Relations and their base weights.  Relations named in the seed's plan (seeds.PLAN, by
change type) keep their weight; others are halved, so a body change still offers its
callers, just after its callees:

    callee_on_changed_line  seed -> X referenced on a changed line          1.0
    caller                  X -> seed                                         0.9
    override / overridden   OVERRIDES in either direction (contracts)         0.8
    callee                  other seed -> X references                        0.6
    tests / tested          TESTS in either direction                         0.5
    importer                module seeds: modules importing it                0.5
    parent                  the class of a changed method (attributes, bases) 0.4

Candidate score = relation weight x plan multiplier x edge confidence (calibrated tier
confidence, design §3.4).  Same-file symbols are eligible: the diff shows only changed
lines, not the callees next to them.  Module nodes are not offered as candidates
(their 'body' is an outline, so call-site windows cannot be cut from it).
"""
from __future__ import annotations

from backend.kg.model import EdgeType, Kind
from backend.kg.snapshot import GraphSnapshot
from backend.retrieval.seeds import PLAN

REL_WEIGHT = {
    "callee_on_changed_line": 1.0, "caller": 0.9, "override": 0.8, "overridden": 0.8, "callee": 0.6,
    "tests": 0.5, "tested": 0.5, "importer": 0.5, "parent": 0.4,
}
UNPLANNED = 0.5
DEP = (EdgeType.CALLS, EdgeType.REFERENCES)


def expand(snap: GraphSnapshot, seeds, limit: int = 80, rel_weight: dict | None = None,
           use_plan: bool = True, overlay_out: dict | None = None) -> list[tuple[int, float, str]]:
    """``overlay_out``: seed key -> [(base index, edge type, confidence, head-side lines)] — the seed's
    outgoing dependencies resolved on the HEAD version of its file (new calls, new symbols)."""
    weights = {**REL_WEIGHT, **(rel_weight or {})}
    seed_idx = {sd.index for sd in seeds if sd.index is not None}
    best: dict[int, tuple[float, str]] = {}

    for sd in seeds:
        i = sd.index
        ov = (overlay_out or {}).get(sd.key)
        if i is None and ov is None:
            continue  # a symbol added by the change, and no overlay graph available
        plan = set(PLAN.get(sd.change, []))
        if "callee_on_changed_line" in plan:
            plan.add("callee")

        def offer(j: int, rel: str, conf: float):
            if j in seed_idx or snap.symbols[j].kind == Kind.MODULE:
                return
            mult = 1.0 if (not use_plan or rel in plan) else UNPLANNED
            sc = weights[rel] * mult * conf
            if sc > best.get(j, (0.0, ""))[0]:
                best[j] = (sc, rel)

        if ov is not None:
            for j, _t, conf, lines in ov:
                offer(j, "callee_on_changed_line" if sd.lines.intersection(lines) else "callee", conf)
        else:
            for t in DEP:
                for j, conf, _w in snap.neighbors(i, t, "out"):
                    lines = snap.evidence.get((i, j, t), [])
                    offer(j, "callee_on_changed_line" if sd.lines.intersection(lines) else "callee", conf)
        if i is None:
            continue
        for t in DEP:
            for j, conf, _w in snap.neighbors(i, t, "in"):
                offer(j, "caller", conf)
        for j, conf, _w in snap.neighbors(i, EdgeType.OVERRIDES, "out"):
            offer(j, "overridden", conf)  # the seed overrides j: j is the contract it must honour
        for j, conf, _w in snap.neighbors(i, EdgeType.OVERRIDES, "in"):
            offer(j, "override", conf)  # j overrides the seed: affected by its change
        for j, conf, _w in snap.neighbors(i, EdgeType.TESTS, "in"):
            offer(j, "tests", conf)
        for j, conf, _w in snap.neighbors(i, EdgeType.TESTS, "out"):
            offer(j, "tested", conf)
        if snap.symbols[i].kind == Kind.MODULE:
            for j, conf, _w in snap.neighbors(i, EdgeType.IMPORTS, "in"):
                offer(j, "importer", conf)
        p = snap.parent[i]
        if p >= 0 and snap.symbols[p].kind == Kind.CLASS:
            offer(p, "parent", 1.0)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:limit]
    return [(j, sc, f"structural:{rel}") for j, (sc, rel) in ranked]


def rrf(channels: dict[str, list[tuple[int, float, str]]], k: int = 60, weights: dict | None = None):
    """Reciprocal Rank Fusion (Cormack, Clarke & Büttcher 2009): score = sum_c w_c / (k + rank_c)."""
    weights = weights or {}
    score: dict[int, float] = {}
    labels: dict[int, list[str]] = {}
    for name, lst in channels.items():
        w = weights.get(name, 1.0)
        for r, (i, _s, label) in enumerate(lst, 1):
            score[i] = score.get(i, 0.0) + w / (k + r)
            labels.setdefault(i, []).append(label)
    order = sorted(score, key=lambda i: (-score[i], i))
    return [(i, score[i], "+".join(sorted(labels[i]))) for i in order]
