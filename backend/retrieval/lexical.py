"""BM25F over symbol fields (design §5.3, ADR-09).

One document per class/function/method.  Fields: name, qualified name + path, signature,
docstring, body.  Per Robertson, Zaragoza & Taylor (2004), field term frequencies are
length-normalised per field, weighted, summed, and saturated once:

    tf~(t, d) = sum_f  w_f * tf_f(t, d) / (1 - b_f + b_f * len_f(d) / avglen_f)
    score(q, d) = sum_t  qw(t) * idf(t) * tf~ / (k1 + tf~)

with idf(t) = ln(1 + (N - df + .5) / (df + .5)).  Query terms are visited in sorted order and
ties broken by document order, so rankings are deterministic.
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
FIELDS = ("name", "qual", "sig", "doc", "body")
DEFAULT_WEIGHTS = {"name": 3.0, "qual": 1.5, "sig": 2.0, "doc": 1.0, "body": 1.0}


def tokenize(text: str) -> list[str]:
    """Full identifiers (lower-cased) plus their snake/camel sub-tokens; Python keywords dropped.
    Identical to the evaluation baseline tokenizer, so A-vs-S2 differences come from fields and packing."""
    toks = []
    for w in _IDENT.findall(text):
        wl = w.lower()
        if wl not in STOP:
            toks.append(wl)
        parts = [p.lower() for p in _SPLIT.split(w) if p]
        if len(parts) > 1:
            toks.extend(p for p in parts if p not in STOP)
    return toks


class BM25F:
    def __init__(self, docs: list[tuple[int, dict[str, str]]], weights: dict | None = None, k1: float = 1.2, b: float = 0.75):
        self.w = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.k1, self.b = k1, b
        self.ids: list[int] = []
        self.lens: list[dict[str, int]] = []
        self.post: dict[str, list[tuple[int, dict[str, int]]]] = defaultdict(list)
        totals = Counter()
        df = Counter()
        for d, (doc_id, fields) in enumerate(docs):
            self.ids.append(doc_id)
            per_field = {f: Counter(tokenize(fields.get(f, "") or "")) for f in FIELDS}
            lens = {f: sum(c.values()) for f, c in per_field.items()}
            self.lens.append(lens)
            totals.update(lens)
            terms = defaultdict(dict)
            for f, c in per_field.items():
                for t, n in c.items():
                    terms[t][f] = n
            for t, ftf in terms.items():
                self.post[t].append((d, ftf))
            df.update(terms.keys())
        n = max(len(docs), 1)
        self.avg = {f: (totals[f] / n) or 1.0 for f in FIELDS}
        self.idf = {t: math.log(1 + (n - m + 0.5) / (m + 0.5)) for t, m in df.items()}

    def search(self, query: dict[str, float], k: int, exclude: set[int] | None = None, weights: dict | None = None,
               k1: float | None = None, b: float | None = None) -> list[tuple[int, float]]:
        """Top-k (doc id, score).  ``weights`` / ``k1`` / ``b`` override the index defaults for this query
        (the index stores raw per-field term frequencies, so no re-indexing is needed)."""
        exclude = exclude or set()
        sc: dict[int, float] = defaultdict(float)
        k1 = self.k1 if k1 is None else k1
        b = self.b if b is None else b
        w = self.w if weights is None else {**DEFAULT_WEIGHTS, **weights}
        avg = self.avg
        for t in sorted(query):
            idf = self.idf.get(t)
            if idf is None:
                continue
            qw = query[t]
            for d, ftf in self.post[t]:
                if self.ids[d] in exclude:
                    continue
                lens = self.lens[d]
                tf = sum(w[f] * n / (1 - b + b * lens[f] / avg[f]) for f, n in ftf.items())
                sc[d] += qw * idf * tf / (k1 + tf)
        top = heapq.nsmallest(k, sc.items(), key=lambda x: (-x[1], x[0]))
        return [(self.ids[d], s) for d, s in top]


def query_from_lines(lines: list[tuple[str, float]], extra_text: str = "", extra_weight: float = 1.0) -> dict[str, float]:
    """Unique terms; a term's weight is the highest weight of any line it occurs on."""
    q: dict[str, float] = {}
    for text, weight in lines:
        for t in set(tokenize(text)):
            q[t] = max(q.get(t, 0.0), weight)
    for t in set(tokenize(extra_text)):
        q[t] = max(q.get(t, 0.0), extra_weight)
    return q
