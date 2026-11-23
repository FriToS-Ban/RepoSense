"""Statistics for comparing retrieval systems on the same queries.

* ``bootstrap_ci``: percentile bootstrap CI of a mean.
* ``paired_randomization_test``: two-sided Fisher randomization (sign-flip) test on
  per-query differences, recommended for IR evaluation by Smucker, Allan & Carterette
  (CIKM 2007).
* ``holm``: Holm-Bonferroni step-down adjustment for families of comparisons (ablation grids).

All functions are deterministic given ``seed``.
"""
from __future__ import annotations

import random
import statistics
from typing import Sequence


def bootstrap_ci(values: Sequence[float], n_resamples: int = 2000, alpha: float = 0.05, seed: int = 0):
    if not values:
        raise ValueError("no values")
    rng = random.Random(seed)
    n = len(values)
    means = sorted(statistics.fmean(rng.choices(values, k=n)) for _ in range(n_resamples))
    lo = means[int(n_resamples * alpha / 2)]
    hi = means[max(0, int(n_resamples * (1 - alpha / 2)) - 1)]
    return statistics.fmean(values), lo, hi


def paired_randomization_test(a: Sequence[float], b: Sequence[float], n_permutations: int = 10_000, seed: int = 0):
    """Returns (mean(a) - mean(b), two-sided p-value).  ``a`` and ``b`` are per-query scores, same order."""
    if len(a) != len(b) or not a:
        raise ValueError("need two non-empty paired samples")
    diffs = [x - y for x, y in zip(a, b)]
    observed = abs(statistics.fmean(diffs))
    if all(d == 0 for d in diffs):
        return 0.0, 1.0
    rng = random.Random(seed)
    n = len(diffs)
    extreme = 0
    for _ in range(n_permutations):
        s = sum(d if rng.random() < 0.5 else -d for d in diffs)
        if abs(s / n) >= observed - 1e-12:
            extreme += 1
    # add-one smoothing: the observed labelling is one of the permutations
    return statistics.fmean(diffs), (extreme + 1) / (n_permutations + 1)


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values (same order as input)."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted
