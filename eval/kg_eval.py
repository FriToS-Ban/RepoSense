"""Evaluation of the v2 knowledge graph (milestone M1).

    python -m eval.kg_eval stats    # build + store every T1 corpus: cost, invariants, structure, agreement
    python -m eval.kg_eval sample   # draw the gold samples (precision by tier; exhaustive call sites for recall)
    python -m eval.kg_eval score    # score labels -> per-tier precision/recall, write backend/kg/calibration.json

Gold data lives in eval/data/kg_gold/ (see its README for the labelling protocol).
Agreement with the frozen reference resolver is a *consistency* check, not a quality
measure (the production resolver is an extension of it); quality comes from the gold labels.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from eval import corpora, offline

EVAL_DIR = Path(__file__).resolve().parent
GOLD = EVAL_DIR / "data" / "kg_gold"
OUT = EVAL_DIR / "results" / "m1-kg"
SALT = "gold-v1"  # stable hash-based selection: unaffected by unrelated changes to the edge population
PRECISION_QUOTA = {"R1": 40, "R2": 40, "R3": 40, "R4": 40, "R5": 50}
RECALL_FUNCTIONS_PER_CORPUS = 2
SCALING_PARTS = ("starlette", "fastapi", "httpx", "pygithub", "pydantic")


def _load_all():
    return [(s.name, corpora.load_verified(s)) for s in corpora.manifest()]


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - r) / d, (c + r) / d


# ------------------------------------------------------------------ stats
def cmd_stats(out_path: str | None = None):
    cache = EVAL_DIR / ".cache"
    cache.mkdir(exist_ok=True)
    db_path = cache / "m1-kg.sqlite"
    if db_path.exists():
        db_path.unlink()
    offline.bootstrap(db_path)
    from backend.core.database import SessionLocal
    from backend.kg.build import build_from_files, validate
    from backend.kg.model import EdgeType, Flag, Kind, symbol_id
    from backend.kg.store import index_files
    from eval.reference import RefGraph

    db = SessionLocal()
    out = {}
    named = _load_all()
    named.append(("monorepo", [f for n in SCALING_PARTS for f in dict(named)[n]]))
    for name, files in named:
        t0 = time.perf_counter()
        st = index_files(db, f"kg-{name}", files, "pinned")
        t_index = time.perf_counter() - t0
        t0 = time.perf_counter()
        st2 = index_files(db, f"kg-{name}", files, "pinned")
        t_noop = time.perf_counter() - t0
        g = build_from_files(f"kg-{name}", files)
        viol = validate(g)
        S = g.symbols
        fn_ids = [s for s, x in S.items() if x.kind in (Kind.FUNCTION, Kind.METHOD)]
        out_dep = defaultdict(int)
        same_file_calls = 0
        for e in g.edges.values():
            if e.type in (EdgeType.CALLS, EdgeType.REFERENCES):
                out_dep[e.src] += 1
                if e.type == EdgeType.CALLS and S[e.src].path == S[e.dst].path:
                    same_file_calls += 1
        row = {
            "files": g.stats["files"], "loc": sum(f["content"].count("\n") for f in files if f["path"].endswith(".py")),
            "symbols": g.stats["symbols"], "edges": g.stats["edges"], "edges_by_tier": g.stats["edges_by_tier"],
            "invariant_violations": len(viol), "parse_errors": g.stats["parse_errors"],
            "seconds_first_index": round(t_index, 2), "seconds_extract": st.extra["extract_seconds"],
            "seconds_resolve": st.extra["resolve_seconds"], "seconds_noop_reindex": round(t_noop, 2),
            "noop_reindex_changed_rows": st2.changed, "files_reparsed_on_noop": st2.extra["files_reparsed"],
            "frac_functions_with_outgoing_dependency_edges": round(sum(1 for s in fn_ids if out_dep[s]) / max(len(fn_ids), 1), 3),
            "same_file_call_edges": same_file_calls,
            "test_symbols": sum(1 for x in S.values() if x.flags & Flag.IS_TEST),
            "residual_chains": g.stats["residual_chains"],
        }
        if name != "monorepo":
            rg = RefGraph(files)
            sid = lambda k: symbol_id(f"kg-{name}", k[0], k[1])  # noqa: E731
            pairs = {(e.src, e.dst): e.resolution for e in g.edges.values() if e.type in (EdgeType.CALLS, EdgeType.REFERENCES)}
            ref = {(sid(o), sid(t)) for o, lst in rg.refs.items() for t, _ in lst if t != o}
            owners = {sid(o) for o in rg.refs}
            row["agreement_with_reference"] = {
                "reference_pairs": len(ref),
                "recovered": round(sum(1 for p in ref if p in pairs) / max(len(ref), 1), 4),
                "v2_only_pairs_by_tier": dict(sorted(Counter(t for p, t in pairs.items() if p not in ref and p[0] in owners).items())),
            }
        out[name] = row
        print(f"[kg] {name:10s} index {row['seconds_first_index']:5.2f}s  noop {row['seconds_noop_reindex']:4.2f}s  "
              f"violations {row['invariant_violations']}  fn-with-deps {row['frac_functions_with_outgoing_dependency_edges']}", flush=True)
    target = Path(out_path) if out_path else OUT / "kg_stats.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {target}")


# ------------------------------------------------------------------ sampling
def _h(*parts) -> str:
    return hashlib.sha1("\x00".join([SALT, *map(str, parts)]).encode()).hexdigest()


def _context(lines, sym, head):
    """Lines in the owner that bind ``head`` (assignment / annotation / parameter)."""
    out = []
    if not head or head.startswith("<"):
        return out
    pat = re.compile(rf"(^|[\s,(]){re.escape(head)}\s*(:[^=]|=[^=])")
    for ln in range(sym.start_line, min(sym.end_line, sym.start_line + 400) + 1):
        if pat.search(lines[ln - 1]):
            out.append(f"{ln}: {lines[ln - 1].strip()[:140]}")
            if len(out) >= 2:
                break
    return out


def _calls_by_site(resolver) -> dict:
    out = defaultdict(list)
    for o in resolver.occurrences:
        if o["type"] == "CALLS":
            out[(o["owner"], o["line"])].append(o)
    return out


def _predict(occ_by, S, owner_sid, line, func) -> list[str]:
    hits = [o for o in occ_by.get((owner_sid, line), []) if o["chain"] == func or func.endswith(o["chain"].split(".")[-1])]
    return sorted({f"{S[o['target']].path}::{S[o['target']].qualname}|{o['tier']}" for o in hits})


def _live_recall_predictions(rec_items) -> dict:
    """Predictions of the CURRENT resolver for the frozen recall call sites (truth labels are system-independent)."""
    from backend.kg.build import build_from_files
    from backend.kg.model import symbol_id

    loaded = dict(_load_all())
    out = {}
    for corpus in sorted({it["corpus"] for it in rec_items}):
        g, r = build_from_files(corpus, loaded[corpus], collect=True)
        occ_by = _calls_by_site(r)
        for it in rec_items:
            if it["corpus"] == corpus:
                path, qual = it["owner"].split("::")
                out[it["id"]] = _predict(occ_by, g.symbols, symbol_id(corpus, path, qual), it["line"], it["call"])
    return out


def cmd_sample():
    from backend.kg.build import build_from_files
    from backend.kg.model import EdgeType, Kind

    pools = defaultdict(lambda: defaultdict(list))
    recall_items = []
    loaded = _load_all()
    graphs = {}
    for name, files in loaded:
        g, r = build_from_files(name, files, confidence={}, collect=True)
        graphs[name] = g
        text = {f["path"]: f["content"].split("\n") for f in files}
        S = g.symbols
        for o in r.occurrences:
            pools[o["tier"]][name].append(o)
        # recall: every call site of a few randomly chosen functions with >= 4 calls
        cands = []
        for sid, s in sorted(S.items(), key=lambda kv: (kv[1].path, kv[1].qualname)):
            if s.kind not in (Kind.FUNCTION, Kind.METHOD) or s.flags & 2:
                continue
            try:
                node = ast.parse("\n".join(text[s.path][s.start_line - 1 : s.end_line]).replace("\t", "    "))
            except SyntaxError:
                try:
                    import textwrap

                    node = ast.parse(textwrap.dedent("\n".join(text[s.path][s.start_line - 1 : s.end_line])))
                except SyntaxError:
                    continue
            calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
            if 4 <= len(calls) <= 15:
                cands.append((sid, s, calls))
        occ_by = _calls_by_site(r)
        cands.sort(key=lambda c: _h(c[1].path, c[1].qualname))
        for sid, s, calls in cands[:RECALL_FUNCTIONS_PER_CORPUS]:
            for c in sorted(calls, key=lambda c: (c.lineno, c.col_offset)):
                line = s.start_line + c.lineno - 1
                func = ast.unparse(c.func)
                recall_items.append({
                    "id": f"rc-{len(recall_items):03d}", "corpus": name, "owner": f"{s.path}::{s.qualname}", "line": line,
                    "call": func[:120], "source": text[s.path][line - 1].strip()[:160],
                    "v2": _predict(occ_by, S, sid, line, func),
                })
    items = []
    for tier, quota in PRECISION_QUOTA.items():
        by_corpus = {c: sorted(v, key=lambda o: _h(o["path"], o["line"], o["chain"], o["target"]), reverse=True)
                     for c, v in sorted(pools[tier].items())}
        picked = []
        while len(picked) < quota and any(by_corpus.values()):
            for c in sorted(by_corpus):
                if by_corpus[c] and len(picked) < quota:
                    picked.append((c, by_corpus[c].pop()))
        items += [(tier, c, o) for c, o in picked]
    rows = []
    texts = {name: {f["path"]: f["content"].split("\n") for f in files} for name, files in loaded}
    for tier, corpus, o in items:
        S = graphs[corpus].symbols
        owner, tgt = S[o["owner"]], S[o["target"]]
        lines = texts[corpus][o["path"]]
        head = o["chain"].split(".")[0]
        rows.append({
            "id": f"p-{len(rows):03d}", "corpus": corpus, "tier": tier, "n_candidates": o["n"], "role": o["role"],
            "at": f"{o['path']}:{o['line']}", "owner": owner.qualname or "<module>", "chain": o["chain"], "via": o["via"],
            "source": lines[o["line"] - 1].strip()[:160],
            "target": f"{tgt.path}::{tgt.qualname}", "target_kind": tgt.kind,
            "target_def": f"{tgt.start_line}: {(tgt.signature or tgt.name).splitlines()[0][:140]}",
            "context": _context(lines, owner, head) if tier in ("R4", "R5") else [],
        })
    GOLD.mkdir(parents=True, exist_ok=True)
    for fname, data in (("precision_items.jsonl", rows), ("recall_items.jsonl", recall_items)):
        with open(GOLD / fname, "w", encoding="utf-8", newline="\n") as fh:
            for r in data:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
    print(f"wrote {len(rows)} precision items and {len(recall_items)} recall call sites to {GOLD}")


# ------------------------------------------------------------------ scoring
def _jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def _version() -> str:
    from backend.kg.model import EXTRACTOR_VERSION

    return EXTRACTOR_VERSION


def _recall(rec_items, rec_labels, predictions) -> dict:
    tally = Counter()
    misses = []
    for iid, it in rec_items.items():
        truth = rec_labels.get(iid, {}).get("truth", "unlabelled")
        predicted = {p.split("|")[0]: p.split("|")[1] for p in predictions.get(iid, [])}
        if truth in ("unlabelled", "unsure", "self"):  # self = recursion; self-loops are excluded by design
            tally[truth] += 1
            continue
        if truth == "ext":
            tally["ext_correctly_unresolved" if not predicted else "ext_but_predicted"] += 1
            continue
        tally["in_repo"] += 1
        if truth in predicted:
            tally["found"] += 1
            tally[f"found_{predicted[truth]}"] += 1
        else:
            misses.append({"id": iid, "call": it["call"], "truth": truth, "predicted": sorted(predicted),
                           "note": rec_labels.get(iid, {}).get("note", "")})
        if any(p != truth for p in predicted):
            tally["with_wrong_extra_candidate"] += 1
    k, n = tally["found"], tally["in_repo"]
    lo, hi = wilson(k, n)
    return {"call_sites": len(rec_items), **dict(sorted(tally.items())),
            "recall_in_repo_calls": round(k / n, 3) if n else None,
            "wilson95": [round(lo, 3), round(hi, 3)] if n else None, "misses": misses}


def cmd_score():
    items = {r["id"]: r for r in _jsonl(GOLD / "precision_items.jsonl")}
    labels = {r["id"]: r for r in _jsonl(GOLD / "precision_labels.jsonl")}
    per = defaultdict(Counter)
    per_n = defaultdict(Counter)
    for iid, it in items.items():
        lab = labels.get(iid, {}).get("label", "unlabelled")
        per[it["tier"]][lab] += 1
        if it["tier"] == "R5":
            per_n[min(it["n_candidates"], 3)][lab] += 1
    result = {"precision": {}, "recall": {}}
    confidence = {}
    for tier in sorted(per):
        c = per[tier]
        k, n = c["correct"], c["correct"] + c["incorrect"]
        lo, hi = wilson(k, n)
        post = (k + 1) / (n + 2) if n else None  # Laplace posterior mean: the calibrated confidence
        result["precision"][tier] = {"correct": k, "incorrect": c["incorrect"], "unsure": c["unsure"],
                                     "unlabelled": c["unlabelled"], "precision": round(k / n, 3) if n else None,
                                     "wilson95": [round(lo, 3), round(hi, 3)] if n else None,
                                     "calibrated_confidence": round(post, 3) if post else None}
        if post:
            confidence[tier] = round(post, 3)
    for b, c in sorted(per_n.items()):
        k, n = c["correct"], c["correct"] + c["incorrect"]
        if n:
            result["precision"][f"R5@{b}"] = {"correct": k, "n": n, "precision": round(k / n, 3)}
            confidence[f"R5@{b}"] = round((k + 1) / (n + 2), 3)
    rec_items = {r["id"]: r for r in _jsonl(GOLD / "recall_items.jsonl")}
    rec_labels = {r["id"]: r for r in _jsonl(GOLD / "recall_labels.jsonl")}
    live = _live_recall_predictions(list(rec_items.values()))
    result["recall"] = {
        "at_sampling(kg-py-1.1)": _recall(rec_items, rec_labels, {i: it["v2"] for i, it in rec_items.items()}),
        f"current({_version()})": _recall(rec_items, rec_labels, live),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "gold_scores.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    from backend.kg.build import CALIBRATION_PATH

    CALIBRATION_PATH.write_text(json.dumps({
        "source": "eval/data/kg_gold (python -m eval.kg_eval score)",
        "method": "Laplace posterior mean of per-tier precision on the gold sample; R5@n = by candidate count",
        "confidence": confidence}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "stats":
        cmd_stats(sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--out" else None)
    else:
        {"sample": cmd_sample, "score": cmd_score}[cmd]()
