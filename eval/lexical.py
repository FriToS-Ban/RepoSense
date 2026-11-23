"""Code-aware tokenizer and Okapi BM25 used as the *evaluation baseline* lexical retriever.

This is the Phase-1 baseline (systems S2/S3).  The production BM25F channel
(design doc §5.3) lives in backend/retrieval and is evaluated against this one.

Determinism: query terms are visited in sorted order and ties are broken by
document insertion order, so results do not depend on PYTHONHASHSEED.
"""
from __future__ import annotations

import heapq
import math
import re
from collections import Counter, defaultdict

STOP = frozenset(
    "self cls def return if else elif for in not and or is none true false import from as with try except "
    "raise class pass lambda yield await async while break continue global nonlocal assert del finally".split()
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_SPLIT = re.compile(r"_+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def tokenize(text: str) -> list[str]:
    """Full identifiers (lower-cased) plus their snake/camel sub-tokens; Python keywords dropped."""
    toks = []
    for w in _IDENT.findall(text):
        wl = w.lower()
        if wl not in STOP:
            toks.append(wl)
        parts = [p.lower() for p in _SPLIT.split(w) if p]
        if len(parts) > 1:
            toks.extend(p for p in parts if p not in STOP)
    return toks


class BM25:
    """Okapi BM25 (Robertson & Zaragoza 2009) with idf = ln(1 + (N - n + .5) / (n + .5))."""

    def __init__(self, docs: list[tuple[str, str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.ids: list[str] = []
        self.dl: list[int] = []
        self.post: dict[str, list[tuple[int, int]]] = defaultdict(list)
        df: Counter = Counter()
        for d, (doc_id, text) in enumerate(docs):
            tf = Counter(tokenize(text))
            self.ids.append(doc_id)
            self.dl.append(sum(tf.values()))
            for w, f in tf.items():
                self.post[w].append((d, f))
            df.update(tf.keys())
        n = len(docs)
        self.avgdl = sum(self.dl) / max(n, 1)
        self.idf = {w: math.log(1 + (n - m + 0.5) / (m + 0.5)) for w, m in df.items()}

    def scores(self, text: str) -> dict[int, float]:
        sc: dict[int, float] = defaultdict(float)
        k1, b, avg = self.k1, self.b, self.avgdl
        for w in sorted(set(tokenize(text))):
            idf = self.idf.get(w)
            if idf is None:
                continue
            for d, f in self.post[w]:
                sc[d] += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * self.dl[d] / avg))
        return sc

    def search(self, text: str, k: int) -> list[tuple[str, float]]:
        sc = self.scores(text)
        top = heapq.nsmallest(k, sc.items(), key=lambda x: (-x[1], x[0]))
        return [(self.ids[d], s) for d, s in top]
