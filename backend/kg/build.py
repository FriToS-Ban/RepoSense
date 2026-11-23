"""Build a repository graph from files, and check its consistency invariants (design §3.6)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from backend.kg.extract_python import extract
from backend.kg.model import EdgeType, FileFacts, Graph, Kind, symbol_id
from backend.kg.resolve import Resolver

CALIBRATION_PATH = Path(__file__).with_name("calibration.json")


def load_calibration() -> dict:
    """Tier confidences measured on the gold sample (eval/kg_gold.py); empty -> priors."""
    if CALIBRATION_PATH.exists():
        return json.loads(CALIBRATION_PATH.read_text(encoding="utf-8")).get("confidence", {})
    return {}


def build_graph(repo_id: str, facts: list[FileFacts], confidence: dict | None = None, collect: bool = False):
    """Global resolution + derivation.  Returns the Graph (and the resolver when ``collect``)."""
    t0 = time.perf_counter()
    r = Resolver(repo_id, facts, confidence if confidence is not None else load_calibration(), collect)
    g = r.run()
    g.stats["resolve_seconds"] = round(time.perf_counter() - t0, 3)
    return (g, r) if collect else g


def build_from_files(repo_id: str, files: list[dict], **kw):
    """files: [{"path", "content"}]; non-Python files are ignored."""
    t0 = time.perf_counter()
    facts = [extract(f["path"], f["content"]) for f in files if f["path"].endswith(".py")]
    t1 = time.perf_counter()
    out = build_graph(repo_id, facts, **kw)
    g = out[0] if isinstance(out, tuple) else out
    g.stats["extract_seconds"] = round(t1 - t0, 3)
    return out


def validate(g: Graph) -> list[str]:
    """Invariants 1-4 of the design: referential integrity, containment, identity, OVERRIDES consistency."""
    v = []
    S = g.symbols
    for e in g.edges.values():
        if e.src not in S or e.dst not in S:
            v.append(f"dangling edge {e.type} {e.src}->{e.dst}")
            continue
        if e.src == e.dst:
            v.append(f"self-loop {e.type} {S[e.src].qualname}")
        if not 0.0 < e.confidence <= 1.0:
            v.append(f"confidence out of range {e.confidence} on {e.type}")
        if e.type == EdgeType.IMPORTS and not (S[e.src].kind == S[e.dst].kind == Kind.MODULE):
            v.append(f"IMPORTS between non-modules {S[e.src].qualname}->{S[e.dst].qualname}")
        if e.type == EdgeType.INHERITS and not (S[e.src].kind == S[e.dst].kind == Kind.CLASS):
            v.append(f"INHERITS between non-classes {S[e.src].qualname}->{S[e.dst].qualname}")
        if e.type == EdgeType.OVERRIDES:
            a, b = S[e.src], S[e.dst]
            pa, pb = a.parent_id, b.parent_id
            if a.kind != Kind.METHOD or b.kind != Kind.METHOD or a.name != b.name or pb not in g.mro.get(pa, [])[1:]:
                v.append(f"inconsistent OVERRIDES {a.path}::{a.qualname} -> {b.path}::{b.qualname}")
    seen = set()
    for sid, s in S.items():
        if sid != symbol_id(s.repo_id, s.path, s.qualname):
            v.append(f"non-deterministic id for {s.path}::{s.qualname}")
        if (s.path, s.qualname) in seen:
            v.append(f"duplicate qualname {s.path}::{s.qualname}")
        seen.add((s.path, s.qualname))
        if not 1 <= s.start_line <= s.end_line:
            v.append(f"empty or inverted span {s.path}::{s.qualname}")
        if s.kind == Kind.MODULE:
            if s.parent_id is not None:
                v.append(f"module with parent {s.path}")
            continue
        p = S.get(s.parent_id)
        if p is None:
            v.append(f"missing parent of {s.path}::{s.qualname}")
            continue
        if p.path != s.path or not (p.start_line <= s.start_line and s.end_line <= p.end_line):
            v.append(f"span of {s.path}::{s.qualname} not inside its parent")
        if s.kind == Kind.METHOD and p.kind != Kind.CLASS:
            v.append(f"method {s.qualname} whose parent is a {p.kind}")
    return v
