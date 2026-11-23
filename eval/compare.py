"""Compare two systems on the same queries with paired statistics.

    python -m eval.compare RESULTS_A:SYSTEM_A RESULTS_B:SYSTEM_B [--view sym_xfile]

RESULTS_* is a directory written by ``eval.run`` (it contains per_query.jsonl).  Both
sides may point at the same directory with different systems.  For every
(category, metric) the report gives mean A, mean B, delta = A - B, a 95% bootstrap CI
of the delta, the two-sided paired randomization p-value, and the Holm-adjusted
p-value across all rows of the report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.run import PER_QUERY_METRICS
from eval.stats import bootstrap_ci, holm, paired_randomization_test


def _load(spec: str):
    path, _, system = spec.rpartition(":")
    if not path or not Path(path).is_dir():
        path, system = spec, None
    rows = {}
    with open(Path(path) / "per_query.jsonl", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            rows[r["qid"]] = r
    return rows, system


def compare(a_spec: str, b_spec: str, view: str = "sym_xfile", metrics=PER_QUERY_METRICS) -> list[dict]:
    (ra, sa), (rb, sb) = _load(a_spec), _load(b_spec)
    report = []
    for category in sorted({r["category"] for r in ra.values()}):
        qids = [q for q in sorted(ra) if q in rb and ra[q]["category"] == category
                and view in ra[q]["m"].get(sa, {}) and view in rb[q]["m"].get(sb, {})]
        if not qids:
            continue
        for i, metric in enumerate(PER_QUERY_METRICS):
            if metric not in metrics:
                continue
            a = [ra[q]["m"][sa][view][i] for q in qids]
            b = [rb[q]["m"][sb][view][i] for q in qids]
            delta, p = paired_randomization_test(a, b)
            _, lo, hi = bootstrap_ci([x - y for x, y in zip(a, b)])
            report.append({"category": category, "metric": metric, "queries": len(qids), "mean_a": sum(a) / len(a),
                           "mean_b": sum(b) / len(b), "delta": delta, "ci95": (lo, hi), "p": p})
    for row, p_adj in zip(report, holm([r["p"] for r in report])):
        row["p_holm"] = p_adj
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--view", default="sym_xfile")
    args = ap.parse_args(argv)
    rows = compare(args.a, args.b, args.view)
    print(f"A = {args.a}\nB = {args.b}\nview = {args.view}\n")
    print(f"{'category':9s} {'metric':8s} {'n':>4s} {'A':>7s} {'B':>7s} {'A-B':>8s} {'95% CI':>18s} {'p':>8s} {'p_holm':>8s}")
    for r in rows:
        ci = f"[{r['ci95'][0]:+.3f}, {r['ci95'][1]:+.3f}]"
        print(f"{r['category']:9s} {r['metric']:8s} {r['queries']:4d} {r['mean_a']:7.3f} {r['mean_b']:7.3f} "
              f"{r['delta']:+8.3f} {ci:>18s} {r['p']:8.4f} {r['p_holm']:8.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
