"""Multi-hop structural candidates (design §5.3, ADR-06).

Personalized PageRank by local push (Andersen, Chung & Lang 2006, "forward push"): the
restart distribution is spread over the seeds; a node holding residual mass r(u) at least
eps x deg(u) keeps alpha x r(u) as its PageRank estimate and passes the rest to its
neighbours in proportion to the transition weights.  Work is O(1 / (eps x alpha)),
independent of the size of the graph, and hubs are damped for free: a helper called from
200 places passes each caller 1/200 of what reaches it.

The transition graph is typed and direction-aware, the same relations as the 1-hop
expansion (structural.py):

    callee      seed -> X it calls / references        (CALLS, REFERENCES forward)
    caller      X -> seed                               (CALLS, REFERENCES reversed)
    overridden / override, tested / tests               (OVERRIDES, TESTS both ways)
    parent / child                                      (method <-> its class)

with weight(u -> v) = type weight x calibrated edge confidence.  Module nodes are not
traversed (an import edge would put every importer of a package within two hops).

``typed_bfs`` is the ablation: breadth-first expansion to depth 2 where a candidate's score
is the best product of edge weights along a path.
"""
from __future__ import annotations

from collections import deque

from backend.kg.model import EdgeType, Kind
from backend.kg.snapshot import GraphSnapshot

TYPE_WEIGHT = {"callee": 1.0, "caller": 0.9, "overridden": 0.8, "override": 0.8, "tested": 0.5, "tests": 0.5,
               "parent": 0.4, "child": 0.2}
_TYPED = (
    (EdgeType.CALLS, "out", "callee"), (EdgeType.REFERENCES, "out", "callee"),
    (EdgeType.CALLS, "in", "caller"), (EdgeType.REFERENCES, "in", "caller"),
    (EdgeType.OVERRIDES, "out", "overridden"), (EdgeType.OVERRIDES, "in", "override"),
    (EdgeType.TESTS, "out", "tested"), (EdgeType.TESTS, "in", "tests"),
)


class Transitions:
    """Weighted, typed neighbour lists of a snapshot, built lazily per node and memoised."""

    def __init__(self, snap: GraphSnapshot, type_weight: dict | None = None):
        self.snap = snap
        self.w = {**TYPE_WEIGHT, **(type_weight or {})}
        self._rows: dict[int, tuple[list[tuple[int, float, str]], float]] = {}

    def row(self, u: int) -> tuple[list[tuple[int, float, str]], float]:
        hit = self._rows.get(u)
        if hit is not None:
            return hit
        snap, best = self.snap, {}
        for etype, direction, rel in _TYPED:
            for v, conf, _n in snap.neighbors(u, etype, direction):
                if v != u and snap.symbols[v].kind != Kind.MODULE:
                    w = self.w[rel] * conf
                    if w > best.get(v, (0.0, ""))[0]:
                        best[v] = (w, rel)
        p = snap.parent[u]
        if p >= 0 and snap.symbols[p].kind == Kind.CLASS:
            if self.w["parent"] > best.get(p, (0.0, ""))[0]:
                best[p] = (self.w["parent"], "parent")
        if snap.symbols[u].kind == Kind.CLASS:
            for c in snap.children(u):
                if self.w["child"] > best.get(c, (0.0, ""))[0]:
                    best[c] = (self.w["child"], "child")
        out = [(v, w, rel) for v, (w, rel) in sorted(best.items()) if w > 0]
        res = (out, sum(w for _, w, _ in out))
        self._rows[u] = res
        return res


def ppr_push(trans: Transitions, sources: dict[int, float], alpha: float = 0.3, eps: float = 1e-4,
             max_pushes: int = 200_000) -> dict[int, float]:
    """Approximate PPR vector for the restart distribution ``sources`` (node -> mass, summing to 1)."""
    p: dict[int, float] = {}
    r: dict[int, float] = dict(sources)
    queue = deque(sorted(sources))
    queued = set(queue)
    pushes = 0
    while queue and pushes < max_pushes:
        u = queue.popleft()
        queued.discard(u)
        ru = r.get(u, 0.0)
        nbrs, total = trans.row(u)
        if ru < eps * max(1, len(nbrs)):
            continue
        pushes += 1
        p[u] = p.get(u, 0.0) + alpha * ru
        r[u] = 0.0
        if not total:
            continue  # dangling: the walk restarts; the remaining mass leaves the estimate
        spread = (1.0 - alpha) * ru / total
        for v, w, _rel in nbrs:
            rv = r.get(v, 0.0) + spread * w
            r[v] = rv
            if v not in queued and rv >= eps * max(1, len(trans.row(v)[0])):
                queue.append(v)
                queued.add(v)
    return p


def typed_bfs(trans: Transitions, sources: dict[int, float], depth: int = 2) -> dict[int, tuple[float, int, str]]:
    """node -> (best path product, hop count, relation path), up to ``depth`` hops."""
    best: dict[int, tuple[float, int, str]] = {s: (1.0, 0, "") for s in sources}
    frontier = sorted(sources)
    for hop in range(1, depth + 1):
        nxt = set()
        for u in frontier:
            su, _h, pu = best[u]
            for v, w, rel in trans.row(u)[0]:
                sc = su * w
                if v not in best or (best[v][1] == hop and sc > best[v][0]):
                    best[v] = (sc, hop, f"{pu}>{rel}" if pu else rel)
                    nxt.add(v)
        frontier = sorted(nxt)
    return {v: x for v, x in best.items() if x[1] > 0}


def sources_from_seeds(snap: GraphSnapshot, seeds, overlay_out: dict | None = None) -> dict[int, float]:
    """Restart distribution: uniform over seeds in the base snapshot; a seed added by the change
    spreads its share over its HEAD-side dependencies (overlay), if known."""
    mass: dict[int, float] = {}
    per_seed = []
    for sd in seeds:
        if sd.index is not None:
            per_seed.append([sd.index])
        else:
            deps = sorted({j for j, *_ in (overlay_out or {}).get(sd.key, [])})
            if deps:
                per_seed.append(deps)
    for nodes in per_seed:
        for j in nodes:
            mass[j] = mass.get(j, 0.0) + 1.0 / (len(per_seed) * len(nodes))
    return mass


def multihop_candidates(snap: GraphSnapshot, seeds, method: str = "ppr", limit: int = 40, alpha: float = 0.3,
                        eps: float = 1e-4, overlay_out: dict | None = None, type_weight: dict | None = None,
                        min_hops: int = 1, trans: Transitions | None = None) -> list[tuple[int, float, str]]:
    """Ranked (index, score, label) candidates from PPR or typed BFS.  Seeds and module nodes are
    never candidates; ``min_hops=2`` keeps only nodes that are not direct neighbours of a seed."""
    trans = trans or Transitions(snap, type_weight)
    src = sources_from_seeds(snap, seeds, overlay_out)
    if not src:
        return []
    seed_idx = {sd.index for sd in seeds if sd.index is not None}
    direct = set()
    for s in seed_idx:
        direct.update(v for v, _w, _r in trans.row(s)[0])
    if method == "ppr":
        scores = ppr_push(trans, src, alpha, eps)
        labels = {v: "ppr" for v in scores}
    elif method == "bfs":
        b = typed_bfs(trans, src, 2)
        scores = {v: x[0] for v, x in b.items()}
        labels = {v: f"2hop:{x[2]}" if x[1] == 2 else x[2] for v, x in b.items()}
    else:
        raise ValueError(method)
    out = [(v, sc, labels[v]) for v, sc in scores.items()
           if v not in seed_idx and snap.symbols[v].kind != Kind.MODULE and (min_hops < 2 or v not in direct)]
    out.sort(key=lambda x: (-x[1], x[0]))
    return [(v, sc, f"structural:{lab}") for v, sc, lab in out[:limit]]
