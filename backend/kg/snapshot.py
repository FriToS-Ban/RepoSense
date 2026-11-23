"""Immutable in-memory view of a repository graph for traversal (design §3.7).

Symbols are numbered 0..n-1.  For every edge type and direction there is a CSR
adjacency (offsets / targets / confidence / weight in compact ``array`` buffers), so a
neighbourhood lookup is a slice.  ``innermost(path, line)`` maps a source line to the
innermost enclosing symbol, which is how diff hunks are linked to seeds (design §5.2).
"""
from __future__ import annotations

from array import array
from bisect import bisect_right
from collections import defaultdict

from backend.kg.model import Graph, Kind


class CSR:
    __slots__ = ("offsets", "targets", "confidence", "weight")

    def __init__(self, n: int, pairs: list[tuple[int, int, float, int]]):
        pairs.sort(key=lambda p: (p[0], p[1]))
        counts = [0] * (n + 1)
        for s, _, _, _ in pairs:
            counts[s + 1] += 1
        for i in range(n):
            counts[i + 1] += counts[i]
        self.offsets = array("i", counts)
        self.targets = array("i", (p[1] for p in pairs))
        self.confidence = array("f", (p[2] for p in pairs))
        self.weight = array("i", (p[3] for p in pairs))

    def row(self, i: int) -> list[tuple[int, float, int]]:
        a, b = self.offsets[i], self.offsets[i + 1]
        return [(self.targets[k], self.confidence[k], self.weight[k]) for k in range(a, b)]


class GraphSnapshot:
    def __init__(self, graph: Graph):
        syms = sorted(graph.symbols.values(), key=lambda s: (s.path, s.start_line, s.qualname))
        self.repo_id = graph.repo_id
        self.ids = [s.id for s in syms]
        self.index = {sid: i for i, sid in enumerate(self.ids)}
        self.symbols = syms
        self.parent = array("i", (self.index.get(s.parent_id, -1) if s.parent_id else -1 for s in syms))
        self.mro = {self.index[c]: [self.index[x] for x in m if x in self.index] for c, m in graph.mro.items() if c in self.index}
        n = len(syms)
        per_type: dict[str, list] = defaultdict(list)
        for e in graph.edges.values():
            if e.src in self.index and e.dst in self.index:
                per_type[e.type].append((self.index[e.src], self.index[e.dst], e.confidence, e.weight))
        self.out = {t: CSR(n, list(p)) for t, p in per_type.items()}
        self.inc = {t: CSR(n, [(d, s, c, w) for s, d, c, w in p]) for t, p in per_type.items()}
        self._by_path: dict[str, tuple[list[int], list[int]]] = {}
        groups = defaultdict(list)
        for i, s in enumerate(syms):
            groups[s.path].append(i)
        for path, idxs in groups.items():
            idxs.sort(key=lambda i: (syms[i].start_line, -syms[i].end_line))
            self._by_path[path] = ([syms[i].start_line for i in idxs], idxs)

    def __len__(self) -> int:
        return len(self.ids)

    def neighbors(self, i: int, etype: str, direction: str = "out") -> list[tuple[int, float, int]]:
        csr = (self.out if direction == "out" else self.inc).get(etype)
        return csr.row(i) if csr else []

    def children(self, i: int) -> list[int]:
        return [j for j, p in enumerate(self.parent) if p == i]

    def module_of(self, path: str) -> int | None:
        entry = self._by_path.get(path)
        if not entry:
            return None
        return next((i for i in entry[1] if self.symbols[i].kind == Kind.MODULE), None)

    def innermost(self, path: str, line: int) -> int | None:
        """Smallest symbol span containing ``line`` (the module if no def/class contains it)."""
        entry = self._by_path.get(path)
        if not entry:
            return None
        starts, idxs = entry
        best = None
        for k in range(bisect_right(starts, line) - 1, -1, -1):
            s = self.symbols[idxs[k]]
            if s.start_line <= line <= s.end_line:
                if best is None or (s.end_line - s.start_line) < (self.symbols[best].end_line - self.symbols[best].start_line):
                    best = idxs[k]
        return best
