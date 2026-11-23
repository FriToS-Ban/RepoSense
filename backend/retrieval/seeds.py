"""Diff -> seeds: link changed lines to the symbols they change (design §5.2).

A seed is a symbol touched by the diff, with a change type that drives the query plan:
body_changed, signature_changed, added, removed, test_changed, module.  New-side lines
are mapped through the head version of the file (extracted on the fly); removed lines
through the base snapshot.  ``structural_change`` reports whether the head differs from
the base at the AST level (comments and formatting do not count), i.e. whether an
overlay graph would be needed for structural expansion (M3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from backend.kg.extract_python import extract
from backend.kg.model import Flag, Kind, SymbolFact
from backend.kg.snapshot import GraphSnapshot
from backend.retrieval.diff import FileDiff

# Relation priorities per change type (design §5.2).  Consumed by structural expansion (M3)
# and by packing (rendering choice); recorded in the trace.
PLAN = {
    "body_changed": ["callee_on_changed_line", "callee", "overridden", "tests"],
    "signature_changed": ["caller", "override", "callee_on_changed_line", "tests"],
    "removed": ["caller", "override", "tests"],
    "added": ["callee", "sibling", "overridden"],
    "test_changed": ["tested", "callee"],
    "module": ["importer", "referenced"],
}


@dataclass
class Seed:
    path: str
    qualname: str
    name: str
    kind: str
    change: str
    index: int | None  # base snapshot index (None for symbols added by the change)
    lines: set[int] = field(default_factory=set)  # changed lines (new side; old side for removals)
    text: str = ""  # head version (base version for removed symbols)
    query_lines: list[tuple[str, float]] = field(default_factory=list)  # (line text, weight) for lexical queries

    @property
    def key(self) -> str:
        return f"{self.path}::{self.qualname}"


@dataclass
class LinkedChange:
    seeds: list[Seed]
    changed_paths: set[str]
    structural_change: bool
    skipped_files: list[str]


def _innermost_fact(symbols: list[SymbolFact], line: int) -> SymbolFact | None:
    best, best_len = None, None
    for s in symbols:
        for a, b in s.spans:
            if a <= line <= b and (best is None or b - a < best_len):
                best, best_len = s, b - a
    return best


def _header_lines(sym: SymbolFact) -> set[int]:
    """Decorator lines + the def/class line(s) of the symbol's first span."""
    n_sig = max(1, (sym.signature or "").count("\n") + 1)
    start = sym.spans[0][0]
    return set(range(start, start + len(sym.decorators) + n_sig))


def link(snap: GraphSnapshot, files: list[FileDiff], head_content: Callable[[str], str | None]) -> LinkedChange:
    seeds: dict[tuple[str, str], Seed] = {}
    changed_paths, skipped = set(), []
    structural = False
    for fd in files:
        path = fd.path
        if fd.binary or not path.endswith(".py"):
            skipped.append(path)
            continue
        changed_paths.update(p for p in (fd.old_path, fd.new_path) if p)
        head_text = head_content(fd.new_path) if fd.new_path and fd.status != "deleted" else None
        head = extract(fd.new_path, head_text) if head_text is not None else None
        head_syms = {s.qualname: s for s in head.symbols} if head else {}
        base_syms = {snap.symbols[i].qualname: i for i in snap.symbols_in(fd.old_path or "")}
        if head is not None:
            if set(head_syms) != set(base_syms) or any(
                    head_syms[q].content_hash != snap.symbols[i].content_hash for q, i in base_syms.items() if q in head_syms):
                structural = True
        elif base_syms:
            structural = True

        def seed_for(qual: str, side_path: str) -> Seed:
            key = (side_path, qual)
            if key not in seeds:
                hs, bi = head_syms.get(qual), base_syms.get(qual)
                ref = hs if hs is not None else snap.symbols[bi]
                kind = ref.kind
                seeds[key] = Seed(side_path, qual, ref.name, kind, "", bi,
                                  text=(hs.body if hs is not None else snap.symbols[bi].body))
            return seeds[key]

        for h in fd.hunks:
            for tag, old_no, new_no, text in h.lines:
                if tag == "+" and head is not None:
                    s = _innermost_fact(head.symbols, new_no)
                    if s is not None:
                        sd = seed_for(s.qualname, fd.new_path)
                        sd.lines.add(new_no)
                        sd.query_lines.append((text, 1.0))
                elif tag == "-" and fd.old_path:
                    i = snap.innermost(fd.old_path, old_no)
                    if i is not None:
                        q = snap.symbols[i].qualname
                        sd = seed_for(q, fd.new_path if q in head_syms else fd.old_path)
                        sd.lines.add(old_no)
                        sd.query_lines.append((text, 1.0))
            # unchanged context lines inside a seed help the lexical query (signature, surrounding code)
            for tag, old_no, new_no, text in h.lines:
                if tag == " " and head is not None:
                    s = _innermost_fact(head.symbols, new_no)
                    if s is not None and (fd.new_path, s.qualname) in seeds:
                        seeds[(fd.new_path, s.qualname)].query_lines.append((text, 0.5))
        for (p, q), sd in seeds.items():
            if sd.change or p not in (fd.new_path, fd.old_path):
                continue
            hs, bi = head_syms.get(q), base_syms.get(q)
            if bi is None:
                sd.change = "added"
            elif hs is None:
                sd.change = "removed"
            elif sd.kind == Kind.MODULE:
                sd.change = "module"
            elif hs.flags & Flag.IS_TEST:
                sd.change = "test_changed"
            elif hs.signature_hash != snap.symbols[bi].signature_hash or (sd.lines & _header_lines(hs)):
                sd.change = "signature_changed"
            else:
                sd.change = "body_changed"
    ordered = sorted(seeds.values(), key=lambda s: (s.path, min(s.lines) if s.lines else 0))
    return LinkedChange(ordered, changed_paths, structural, skipped)
