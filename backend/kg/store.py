"""SQL persistence of the knowledge graph by transactional diff-apply (design §4.4).

``apply_graph`` makes the stored graph of a repository equal to a freshly built Graph:
it computes insert / update / delete sets for symbols and edges, writes them together
with vector-outbox rows and the snapshot flip in ONE transaction, and touches nothing
that did not change.  Orphaned vectors are impossible by construction: every deleted or
content-changed symbol enqueues an outbox operation in the same transaction.

``index_files`` is the entry point: extraction cache (kg_files) -> build -> validate -> apply.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from sqlalchemy import bindparam, delete, insert, select, update
from sqlalchemy.orm import Session

from backend.kg.build import build_graph, validate
from backend.kg.extract_python import content_hash, extract
from backend.kg.model import EXTRACTOR_VERSION, Edge, FileFacts, Graph, Symbol
from backend.models.models import KGEdge, KGFile, KGSnapshot, KGSymbol, KGVectorOutbox

SYMBOL_COLS = ("repo_id", "path", "qualname", "name", "kind", "parent_id", "start_line", "end_line",
               "signature", "docstring", "body", "flags", "content_hash", "signature_hash")
EDGE_COLS = ("repo_id", "confidence", "resolution", "role", "weight", "evidence_json")
CHUNK = 500


class KGInvariantError(RuntimeError):
    pass


@dataclass
class ApplyStats:
    symbols_inserted: int = 0
    symbols_updated: int = 0
    symbols_deleted: int = 0
    edges_inserted: int = 0
    edges_updated: int = 0
    edges_deleted: int = 0
    outbox_rows: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return any((self.symbols_inserted, self.symbols_updated, self.symbols_deleted,
                    self.edges_inserted, self.edges_updated, self.edges_deleted))


def _symbol_row(s: Symbol) -> dict:
    return {"id": s.id, **{c: getattr(s, c) for c in SYMBOL_COLS}}


def _edge_row(e: Edge, repo_id: str) -> dict:
    evidence = json.dumps({"lines": e.lines, "roles": sorted(set(e.roles))}, separators=(",", ":"))
    return {"src_id": e.src, "dst_id": e.dst, "type": e.type, "repo_id": repo_id, "confidence": round(e.confidence, 6),
            "resolution": e.resolution, "role": e.role, "weight": e.weight, "evidence_json": evidence}


def _chunks(seq, n=CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def apply_graph(db: Session, graph: Graph, commit_sha: str, stats_json: dict | None = None, commit: bool = True) -> ApplyStats:
    repo = graph.repo_id
    st = ApplyStats()
    cur_syms = {r.id: r for r in db.execute(select(KGSymbol).where(KGSymbol.repo_id == repo)).scalars()}
    new_syms = {sid: _symbol_row(s) for sid, s in graph.symbols.items()}
    sym_ins = [r for sid, r in new_syms.items() if sid not in cur_syms]
    sym_del = [sid for sid in cur_syms if sid not in new_syms]
    sym_upd = [r for sid, r in new_syms.items()
               if sid in cur_syms and any(getattr(cur_syms[sid], c) != r[c] for c in SYMBOL_COLS)]

    cur_edges = {(r.src_id, r.dst_id, r.type): r for r in db.execute(select(KGEdge).where(KGEdge.repo_id == repo)).scalars()}
    new_edges = {k: _edge_row(e, repo) for k, e in graph.edges.items()}
    edge_ins = [r for k, r in new_edges.items() if k not in cur_edges]
    edge_del = [k for k in cur_edges if k not in new_edges]
    edge_upd = [r for k, r in new_edges.items() if k in cur_edges and any(getattr(cur_edges[k], c) != r[c] for c in EDGE_COLS)]

    outbox = [{"repo_id": repo, "symbol_id": sid, "op": "delete", "content_hash": None} for sid in sym_del]
    outbox += [{"repo_id": repo, "symbol_id": r["id"], "op": "upsert", "content_hash": r["content_hash"]}
               for r in sym_ins + [r for r in sym_upd if r["content_hash"] != cur_syms[r["id"]].content_hash]]

    # Core-level executemany statements: ORM-enabled UPDATE/DELETE would re-evaluate their criteria
    # against every object loaded above (O(changes x rows); 94 s for one click commit before M7).
    te, ts = KGEdge.__table__, KGSymbol.__table__
    edge_key = (te.c.src_id == bindparam("k_src")) & (te.c.dst_id == bindparam("k_dst")) & (te.c.type == bindparam("k_type"))
    if edge_del:
        db.execute(te.delete().where(edge_key), [{"k_src": s, "k_dst": d, "k_type": t} for s, d, t in edge_del])
    for ids in _chunks(sym_del):
        db.execute(ts.delete().where(ts.c.id.in_(ids)))
    if sym_ins:
        db.execute(ts.insert(), sym_ins)
    if sym_upd:
        db.execute(ts.update().where(ts.c.id == bindparam("k_id")).values({c: bindparam(c) for c in SYMBOL_COLS}),
                   [{**{c: r[c] for c in SYMBOL_COLS}, "k_id": r["id"]} for r in sym_upd])
    if edge_ins:
        db.execute(te.insert(), edge_ins)
    if edge_upd:
        db.execute(te.update().where(edge_key).values({c: bindparam(c) for c in EDGE_COLS}),
                   [{**{c: r[c] for c in EDGE_COLS}, "k_src": r["src_id"], "k_dst": r["dst_id"], "k_type": r["type"]}
                    for r in edge_upd])
    if outbox:
        db.execute(insert(KGVectorOutbox), outbox)
    db.expire_all()  # the Core statements bypassed the identity map: reload anything read later in this session

    db.execute(update(KGSnapshot).where(KGSnapshot.repo_id == repo).values(is_current=False))
    snap = db.get(KGSnapshot, (repo, commit_sha))
    payload = json.dumps(stats_json or graph.stats, sort_keys=True)
    if snap is None:
        db.add(KGSnapshot(repo_id=repo, commit_sha=commit_sha, extractor_version=EXTRACTOR_VERSION, is_current=True,
                          stats_json=payload))
    else:
        snap.is_current, snap.extractor_version, snap.stats_json = True, EXTRACTOR_VERSION, payload
    if commit:
        db.commit()
    st.symbols_inserted, st.symbols_updated, st.symbols_deleted = len(sym_ins), len(sym_upd), len(sym_del)
    st.edges_inserted, st.edges_updated, st.edges_deleted = len(edge_ins), len(edge_upd), len(edge_del)
    st.outbox_rows = len(outbox)
    return st


def index_files(db: Session, repo_id: str, files: list[dict], commit_sha: str) -> ApplyStats:
    """Index a full file set of a repository at ``commit_sha`` (non-Python files are ignored).

    Unchanged files reuse cached FileFacts; resolution always runs globally (design ADR-08).
    Raises KGInvariantError, writing nothing, if the built graph violates an invariant."""
    t0 = time.perf_counter()
    cached = {r.path: r for r in db.execute(select(KGFile).where(KGFile.repo_id == repo_id)).scalars()}
    facts, fresh = [], []
    for f in files:
        if not f["path"].endswith(".py"):
            continue
        h = content_hash(f["content"])
        row = cached.get(f["path"])
        if row is not None and row.content_hash == h and row.extractor_version == EXTRACTOR_VERSION:
            facts.append(FileFacts.from_json(row.facts_json))
        else:
            ff = extract(f["path"], f["content"])
            facts.append(ff)
            fresh.append(ff)
    present = {ff.path for ff in facts}
    t1 = time.perf_counter()
    graph = build_graph(repo_id, facts)
    violations = validate(graph)
    if violations:
        raise KGInvariantError(f"{len(violations)} invariant violations, e.g. {violations[:3]}")
    for ff in fresh:
        row = {"content_hash": ff.content_hash, "extractor_version": EXTRACTOR_VERSION, "facts_json": ff.to_json()}
        if ff.path in cached:
            db.execute(update(KGFile).where(KGFile.repo_id == repo_id, KGFile.path == ff.path).values(row))
        else:
            db.execute(insert(KGFile).values(repo_id=repo_id, path=ff.path, **row))
    gone = [p for p in cached if p not in present]
    for paths in _chunks(gone):
        db.execute(delete(KGFile).where(KGFile.repo_id == repo_id, KGFile.path.in_(paths)))
    graph.stats.update({"extract_seconds": round(t1 - t0, 3), "files_reparsed": len(fresh), "files_cached": len(facts) - len(fresh)})
    st = apply_graph(db, graph, commit_sha)
    st.extra = {**graph.stats, "seconds_total": round(time.perf_counter() - t0, 3)}
    return st


def update_files(db: Session, repo_id: str, changes: dict[str, str | None], commit_sha: str) -> ApplyStats:
    """Incremental update (design §4.6): ``changes`` maps every added / modified path to its new
    content and every deleted path to None (a rename is a delete plus an add), e.g. from the
    GitHub compare API between the indexed commit and ``commit_sha``.

    Only the changed files are re-extracted; all other FileFacts come from the extraction cache.
    Resolution then runs globally, so the result equals a full index of ``commit_sha`` (checked
    on real history by eval/kg_update_equivalence.py).  Raises KGInvariantError, writing
    nothing, if the result violates an invariant."""
    t0 = time.perf_counter()
    cached = {r.path: r for r in db.execute(select(KGFile).where(KGFile.repo_id == repo_id)).scalars()}
    facts = {p: None for p in cached}  # path -> FileFacts, loaded lazily below
    fresh: list[FileFacts] = []
    for path, content in sorted(changes.items()):
        if not path.endswith(".py"):
            continue
        if content is None:
            facts.pop(path, None)
            continue
        row = cached.get(path)
        if row is not None and row.content_hash == content_hash(content) and row.extractor_version == EXTRACTOR_VERSION:
            continue  # unchanged after all (e.g. a revert inside the range)
        ff = extract(path, content)
        facts[path] = ff
        fresh.append(ff)
    stale = [p for p, ff in facts.items() if ff is None and cached[p].extractor_version != EXTRACTOR_VERSION]
    if stale:
        raise KGInvariantError(f"extraction cache of {len(stale)} files is from another extractor version; run index_files")
    all_facts = [ff if ff is not None else FileFacts.from_json(cached[p].facts_json) for p, ff in sorted(facts.items())]
    t1 = time.perf_counter()
    graph = build_graph(repo_id, all_facts)
    violations = validate(graph)
    if violations:
        raise KGInvariantError(f"{len(violations)} invariant violations, e.g. {violations[:3]}")
    for ff in fresh:
        row = {"content_hash": ff.content_hash, "extractor_version": EXTRACTOR_VERSION, "facts_json": ff.to_json()}
        if ff.path in cached:
            db.execute(update(KGFile).where(KGFile.repo_id == repo_id, KGFile.path == ff.path).values(row))
        else:
            db.execute(insert(KGFile).values(repo_id=repo_id, path=ff.path, **row))
    gone = [p for p in cached if p not in facts]
    for paths in _chunks(gone):
        db.execute(delete(KGFile).where(KGFile.repo_id == repo_id, KGFile.path.in_(paths)))
    graph.stats.update({"extract_seconds": round(t1 - t0, 3), "files_reparsed": len(fresh),
                        "files_cached": len(all_facts) - len(fresh), "files_deleted": len(gone)})
    st = apply_graph(db, graph, commit_sha)
    st.extra = {**graph.stats, "seconds_total": round(time.perf_counter() - t0, 3)}
    return st


def load_graph(db: Session, repo_id: str) -> Graph:
    """Read the stored graph back (used by tests and by the snapshot builder)."""
    g = Graph(repo_id)
    for r in db.execute(select(KGSymbol).where(KGSymbol.repo_id == repo_id)).scalars():
        g.symbols[r.id] = Symbol(r.id, r.repo_id, r.path, r.qualname, r.name, r.kind, r.parent_id, r.start_line,
                                 r.end_line, r.signature, r.docstring, r.body, r.flags, r.content_hash, r.signature_hash)
    for r in db.execute(select(KGEdge).where(KGEdge.repo_id == repo_id)).scalars():
        ev = json.loads(r.evidence_json or "{}")
        g.edges[(r.src_id, r.dst_id, r.type)] = Edge(r.src_id, r.dst_id, r.type, r.confidence, r.resolution, r.role,
                                                     r.weight, ev.get("lines", []), ev.get("roles", []))
    return g
