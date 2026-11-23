"""Regression gate: compare a fresh evaluation run against committed results.

    python -m eval.gate t1 FRESH_METRICS_JSON COMMITTED_METRICS_JSON
    python -m eval.gate kg FRESH_KG_STATS_JSON COMMITTED_KG_STATS_JSON

Every deterministic field must match (floats within FLOAT_TOL, to absorb libm
differences across platforms); timing fields are ignored.  Any intended change to
retrieval or graph behaviour must update the committed results in the same commit,
and the diff then documents the change.
"""
from __future__ import annotations

import json
import sys

FLOAT_TOL = 1e-3
TIMING_KEYS = {"lat_ms", "latency_ms", "seconds_local_cpu", "seconds_first_index", "seconds_extract",
               "seconds_resolve", "seconds_noop_reindex", "python", "graph_seconds", "index_seconds",
               "link_ms", "candidates_ms", "pack_ms"}


def _diff(a, b, path="", out=None):
    out = [] if out is None else out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k in TIMING_KEYS:
                continue
            if k not in a or k not in b:
                out.append(f"{path}/{k}: {'missing in fresh' if k not in a else 'not in committed'}")
            else:
                _diff(a[k], b[k], f"{path}/{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{i}]", out)
    elif isinstance(a, float) or isinstance(b, float):
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or abs(a - b) > FLOAT_TOL:
            out.append(f"{path}: {a} != {b}")
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")
    return out


def gate_t1(fresh: dict, committed: dict) -> list[str]:
    fresh = {k: v for k, v in fresh.items() if k not in ("meta",)}
    committed = {k: v for k, v in committed.items() if k not in ("meta",)}
    if "s0_as_shipped" in fresh:  # log text contains a process-specific module name only in old runs
        fresh["s0_as_shipped"] = {"returned_context_chars": fresh["s0_as_shipped"]["returned_context_chars"]}
        committed["s0_as_shipped"] = {"returned_context_chars": committed["s0_as_shipped"]["returned_context_chars"]}
    return _diff(fresh, committed)


def gate_kg(fresh: dict, committed: dict) -> list[str]:
    problems = [f"/{c}: {r['invariant_violations']} invariant violations" for c, r in fresh.items() if r["invariant_violations"]]
    return problems + _diff(fresh, committed)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    kind, fresh_path, committed_path = argv
    fresh = json.load(open(fresh_path, encoding="utf-8"))
    committed = json.load(open(committed_path, encoding="utf-8"))
    problems = {"t1": gate_t1, "kg": gate_kg}[kind](fresh, committed)
    if problems:
        print(f"REGRESSION GATE FAILED ({kind}): {len(problems)} difference(s)")
        for p in problems[:50]:
            print("  " + p)
        return 1
    print(f"regression gate passed ({kind})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
