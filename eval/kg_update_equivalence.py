"""M7 exit criterion: update(index(A), A->B) == index(B) on real commit pairs, zero orphans.

    python -m eval.kg_update_equivalence          # -> eval/results/m7-update/equivalence.json

For each T2 repository: index the code files at the start of a first-parent chain of up to
CHAIN_LENGTH commits ending at the pin, then walk the chain until PAIRS_PER_REPO steps that
change at least one code file have been checked (steps that change none are skipped).  For
every such step A -> B:

  1. the compare-style change set (added / modified paths with their content at B, deleted
     paths -> None) is applied with ``kg.store.update_files``;
  2. the stored graph is read back and compared field by field with a graph built from
     scratch at B (symbols: every column; edges: type, confidence to 1e-6, tier, role,
     weight, evidence lines, roles);
  3. orphan checks: edges whose endpoints are not stored, extraction-cache rows for files
     that no longer exist (or missing for files that do), invariant violations, and the
     vector outbox: every deleted symbol has a 'delete' row and every new or
     content-changed symbol an 'upsert' row written in the same transaction.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from eval import offline

EVAL_DIR = Path(__file__).resolve().parent
PAIRS_PER_REPO = 40  # commit pairs that change at least one code file
CHAIN_LENGTH = 400  # first-parent commits walked at most (steps that change no code file are skipped)
SYMBOL_FIELDS = ("path", "qualname", "name", "kind", "parent_id", "start_line", "end_line", "signature", "docstring",
                 "body", "flags", "content_hash", "signature_hash")


def _edge_sig(e) -> tuple:
    return (round(e.confidence, 6), e.resolution, e.role, e.weight, list(e.lines), sorted(set(e.roles)))


def compare_graphs(stored, fresh) -> list[str]:
    out = []
    a, b = set(stored.symbols), set(fresh.symbols)
    out += [f"symbol only in update: {x}" for x in sorted(a - b)[:5]]
    out += [f"symbol only in full index: {x}" for x in sorted(b - a)[:5]]
    for sid in sorted(a & b):
        sa, sb = stored.symbols[sid], fresh.symbols[sid]
        diff = [f for f in SYMBOL_FIELDS if getattr(sa, f) != getattr(sb, f)]
        if diff:
            out.append(f"symbol {sa.path}::{sa.qualname} differs in {diff}")
    ea, eb = set(stored.edges), set(fresh.edges)
    out += [f"edge only in update: {x}" for x in sorted(ea - eb)[:5]]
    out += [f"edge only in full index: {x}" for x in sorted(eb - ea)[:5]]
    for k in sorted(ea & eb):
        if _edge_sig(stored.edges[k]) != _edge_sig(fresh.edges[k]):
            out.append(f"edge {k} differs: {_edge_sig(stored.edges[k])} != {_edge_sig(fresh.edges[k])}")
    return out


def first_parent_chain(repo, pin: str, n: int) -> list[str]:
    from eval.benchmarks import t2

    return list(reversed(t2.git(repo, "rev-list", "--first-parent", f"--max-count={n + 1}", pin).split()))


def run(pairs_per_repo: int = PAIRS_PER_REPO, repos=None) -> dict:
    cache = EVAL_DIR / ".cache"
    cache.mkdir(exist_ok=True)
    db_path = cache / "m7-equivalence.sqlite"
    if db_path.exists():
        db_path.unlink()
    offline.bootstrap(db_path)
    from sqlalchemy import func, select

    from backend.core.database import SessionLocal
    from backend.kg.build import build_graph, validate
    from backend.kg.store import index_files, load_graph, update_files
    from backend.models.models import KGEdge, KGFile, KGSymbol, KGVectorOutbox
    from eval.benchmarks import t2
    from eval.run_t2 import Snapshotter

    db = SessionLocal()
    report = {"pairs": 0, "pairs_equivalent": 0, "problems": [], "per_repo": {}}
    for spec in t2.manifest():
        if repos and spec["name"] not in repos:
            continue
        repo = t2.ensure_clone(spec)
        snapper = Snapshotter(repo)
        rid = f"m7-{spec['name']}"
        chain = first_parent_chain(repo, spec["pin"], CHAIN_LENGTH)
        prev = dict(snapper.files_at(chain[0]))
        snapper._read_blobs(sorted(prev.values()))
        index_files(db, rid, [{"path": p, "content": snapper.text[b]} for p, b in sorted(prev.items())], chain[0])
        stats = {"pairs": 0, "equivalent": 0, "files_changed": [], "update_s": [], "full_build_s": [],
                 "symbols_touched": [], "commits_walked": 0}
        for a, b in zip(chain, chain[1:]):
            if stats["pairs"] >= pairs_per_repo:
                break
            stats["commits_walked"] += 1
            cur = dict(snapper.files_at(b))
            changes = {p: blob for p, blob in cur.items() if prev.get(p) != blob}
            changes.update({p: None for p in prev if p not in cur})
            if not changes:
                continue  # no code file changed (docs, CI, ...): nothing to test
            snapper._read_blobs(sorted(set(cur.values())))
            changes = {p: (None if blob is None else snapper.text[blob]) for p, blob in changes.items()}
            outbox0 = db.scalar(select(func.count()).select_from(KGVectorOutbox).where(KGVectorOutbox.repo_id == rid))
            before = {r.id: r.content_hash for r in db.execute(select(KGSymbol).where(KGSymbol.repo_id == rid)).scalars()}
            t0 = time.perf_counter()
            st = update_files(db, rid, changes, b)
            t_upd = time.perf_counter() - t0
            stored = load_graph(db, rid)
            facts, _contents = snapper.facts_at(b)
            t1 = time.perf_counter()
            fresh = build_graph(rid, facts)
            t_full = time.perf_counter() - t1
            problems = compare_graphs(stored, fresh)
            stored.mro = fresh.mro  # the MRO is derived, not stored; the OVERRIDES invariant needs it
            problems += [f"invariant: {v}" for v in validate(stored)[:5]]
            sym_ids = set(stored.symbols)
            dangling = [k for k in db.execute(select(KGEdge.src_id, KGEdge.dst_id).where(KGEdge.repo_id == rid)).all()
                        if k[0] not in sym_ids or k[1] not in sym_ids]
            if dangling:
                problems.append(f"{len(dangling)} orphaned edges")
            cached = set(db.scalars(select(KGFile.path).where(KGFile.repo_id == rid)))
            py = {p for p in cur if p.endswith(".py")}
            if cached != py:
                problems.append(f"extraction cache mismatch: {len(cached - py)} stale, {len(py - cached)} missing")
            after = {sid: s.content_hash for sid, s in stored.symbols.items()}
            want_del = {sid for sid in before if sid not in after}
            want_up = {sid for sid, h in after.items() if before.get(sid) != h}
            rows = db.execute(select(KGVectorOutbox.symbol_id, KGVectorOutbox.op)
                              .where(KGVectorOutbox.repo_id == rid).order_by(KGVectorOutbox.id).offset(outbox0)).all()
            got_del = {s for s, op in rows if op == "delete"}
            got_up = {s for s, op in rows if op == "upsert"}
            if got_del != want_del or got_up != want_up:
                problems.append(f"outbox mismatch: delete {len(got_del)}/{len(want_del)}, upsert {len(got_up)}/{len(want_up)}")
            stats["pairs"] += 1
            stats["equivalent"] += int(not problems)
            stats["files_changed"].append(len(changes))
            stats["symbols_touched"].append(st.symbols_inserted + st.symbols_updated + st.symbols_deleted)
            stats["update_s"].append(t_upd)
            stats["full_build_s"].append(t_full)
            if problems:
                report["problems"].append({"repo": spec["name"], "from": a, "to": b, "problems": problems[:10]})
            prev = cur
        report["pairs"] += stats["pairs"]
        report["pairs_equivalent"] += stats["equivalent"]
        report["per_repo"][spec["name"]] = {
            "pairs": stats["pairs"], "equivalent": stats["equivalent"], "commits_walked": stats["commits_walked"],
            "total_files_changed": sum(stats["files_changed"]),
            "median_files_changed": statistics.median(stats["files_changed"]),
            "median_symbols_touched": statistics.median(stats["symbols_touched"]),
            "median_update_seconds": round(statistics.median(stats["update_s"]), 3),
            "median_full_build_seconds": round(statistics.median(stats["full_build_s"]), 3),
            "symbols": len(stored.symbols), "edges": len(stored.edges),
        }
        print(f"[m7] {spec['name']}: {stats['equivalent']}/{stats['pairs']} equivalent", flush=True)
    db.close()
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(EVAL_DIR / "results" / "m7-update" / "equivalence.json"))
    ap.add_argument("--pairs", type=int, default=PAIRS_PER_REPO)
    ap.add_argument("--repos", default="")
    args = ap.parse_args(argv)
    rep = run(args.pairs, [r for r in args.repos.split(",") if r] or None)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k != "problems"}, indent=1, sort_keys=True))
    print(f"problems: {len(rep['problems'])}")
    for p in rep["problems"][:10]:
        print(" ", p)
    return 0 if rep["pairs"] and rep["pairs"] == rep["pairs_equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
