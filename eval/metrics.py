"""Ranked-retrieval metrics.

All functions take a ranked list of document keys (duplicates allowed; only the first
occurrence counts) and graded relevance judgments ``{key: grade}`` with grade > 0 for
relevant documents.  Definitions:

  P@K     = |relevant in top K| / K
  R@K     = |relevant in top K| / |relevant|
  Hit@K   = 1 if any relevant in top K
  MRR     = 1 / rank of first relevant (0 if none), over the whole list
  nDCG@K  = DCG@K / IDCG@K, gain 2^g - 1, discount log2(rank + 1)   (Järvelin & Kekäläinen 2002)
  R@all / P@all / Hit@all  = the same over the whole returned list (used for packed contexts,
                             whose length is set by a character budget rather than K)
"""
from __future__ import annotations

import math
import statistics
from typing import Hashable, Iterable, Sequence

DEFAULT_KS = (5, 10, 20)


def dedupe(ranked: Iterable[Hashable]) -> list:
    seen, out = set(), []
    for k in ranked:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def dcg(gains: Sequence[float]) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def ir_metrics(ranked: Iterable[Hashable], grades: dict, ks: Sequence[int] = DEFAULT_KS) -> dict | None:
    """Returns None when there are no judged-relevant documents (query is skipped for that view)."""
    if not grades:
        return None
    r = dedupe(ranked)
    rel = [grades.get(k, 0) for k in r]
    n_rel = len(grades)
    ideal = sorted(grades.values(), reverse=True)
    out: dict[str, float] = {}
    for k in ks:
        top = rel[:k]
        hits = sum(1 for g in top if g > 0)
        out[f"P@{k}"] = hits / k
        out[f"R@{k}"] = hits / n_rel
        out[f"Hit@{k}"] = 1.0 if hits else 0.0
        idcg = dcg(ideal[:k])
        out[f"nDCG@{k}"] = dcg(top) / idcg if idcg else 0.0
    first = next((i for i, g in enumerate(rel) if g > 0), None)
    out["MRR"] = 1.0 / (first + 1) if first is not None else 0.0
    hits = sum(1 for g in rel if g > 0)
    out["R@all"] = hits / n_rel
    out["P@all"] = hits / len(r) if r else 0.0
    out["Hit@all"] = 1.0 if hits else 0.0
    out["n"] = float(len(r))
    return out


def mean_metrics(rows: list[dict]) -> dict:
    """Macro-average over queries (each query weighs the same), rounded to 4 decimals."""
    keys = sorted({k for r in rows for k, v in r.items() if isinstance(v, (int, float))})
    return {k: round(statistics.mean(r[k] for r in rows if k in r), 4) for k in keys}
