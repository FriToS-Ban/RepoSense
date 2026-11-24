"""File-level co-change index: evolutionary coupling mined from history (design §3.3).

Following Gall et al. (1998) and Zimmermann et al. (2004): two files are coupled when
commits change them together.  For a file ``a`` and a partner ``b``:

    support(a, b)    = number of commits changing both
    confidence(a→b)  = support(a, b) / n(a)      (n(a) = commits changing a)

Commits touching more than ``max_files`` code files are ignored (tangled or mass changes,
Herzig & Zeller 2013), and partners need ``min_support`` co-changes.  The index is
append-only: feed it commits in time order (push events in production; history before
the query commit in evaluation, which prevents leakage).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations


class CoChangeIndex:
    def __init__(self, max_files: int = 20, min_support: int = 2):
        self.max_files, self.min_support = max_files, min_support
        self.n: Counter = Counter()
        self.pair: dict[str, Counter] = defaultdict(Counter)
        self.commits = 0

    def add(self, paths) -> None:
        ps = sorted(set(paths))
        if not 2 <= len(ps) <= self.max_files:
            return
        self.commits += 1
        for a in ps:
            self.n[a] += 1
        for a, b in combinations(ps, 2):
            self.pair[a][b] += 1
            self.pair[b][a] += 1

    def related(self, path: str, k: int = 10) -> list[tuple[str, float]]:
        na = self.n.get(path, 0)
        if not na:
            return []
        out = [(b, s / na, s) for b, s in self.pair[path].items() if s >= self.min_support]
        out.sort(key=lambda x: (-x[1], -x[2], x[0]))
        return [(b, round(c, 4)) for b, c, _ in out[:k]]
