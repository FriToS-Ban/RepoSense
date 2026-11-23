"""Derived structure: method resolution order, OVERRIDES and TESTS edges (design §3.3)."""
from __future__ import annotations

import posixpath
from collections import defaultdict

from backend.kg.model import Edge, EdgeType, Flag, Graph, Kind, Tier

TESTS_DIRECT, TESTS_VIA_HELPER, TESTS_BY_NAME = 1.0, 0.5, 0.3


def c3_mro(cid: str, bases: dict[str, list[str]], _memo: dict | None = None, _stack: set | None = None) -> list[str]:
    """C3 linearisation over in-repo bases (external bases are opaque).  Falls back to a
    left-to-right depth-first order if the hierarchy is inconsistent or cyclic."""
    memo = _memo if _memo is not None else {}
    stack = _stack if _stack is not None else set()
    if cid in memo:
        return memo[cid]
    if cid in stack:
        return [cid]
    stack.add(cid)
    seqs = [list(c3_mro(b, bases, memo, stack)) for b in bases.get(cid, [])] + [list(bases.get(cid, []))]
    seqs = [s for s in seqs if s]
    result = [cid]
    while seqs:
        head = next((s[0] for s in seqs if not any(s[0] in t[1:] for t in seqs)), None)
        if head is None:  # inconsistent hierarchy
            result = [cid] + _dfs(cid, bases)[1:]
            break
        result.append(head)
        for s in seqs:
            if s and s[0] == head:
                s.pop(0)
        seqs = [s for s in seqs if s]
    stack.discard(cid)
    memo[cid] = result
    return result


def _dfs(cid, bases, seen=None):
    seen = seen if seen is not None else set()
    out = []
    if cid in seen:
        return out
    seen.add(cid)
    out.append(cid)
    for b in bases.get(cid, []):
        out += _dfs(b, bases, seen)
    return out


def derive_overrides(resolver) -> None:
    """Method C.m overrides the first B.m found in MRO(C)[1:]."""
    g = resolver.graph
    for cid, members in resolver.members.items():
        mro = g.mro.get(cid, [cid])
        for name, mid in members.items():
            if g.symbols[mid].kind != Kind.METHOD:
                continue
            for b in mro[1:]:
                bm = resolver.members.get(b, {}).get(name)
                if bm and g.symbols[bm].kind == Kind.METHOD:
                    abstract = bool(g.symbols[bm].flags & Flag.ABSTRACT or g.symbols[b].flags & Flag.PROTOCOL)
                    role = "implements_abstract" if abstract else "override"
                    g.edges[(mid, bm, EdgeType.OVERRIDES)] = Edge(mid, bm, EdgeType.OVERRIDES, 1.0, Tier.DERIVED, role, 1,
                                                                   [g.symbols[mid].start_line], [role])
                    break


def derive_tests(g: Graph) -> None:
    """TESTS(test -> target): direct use (1.0), via one helper inside test files (0.5), and
    test-module naming convention tests/test_foo.py -> foo.py when unambiguous (0.3)."""
    out = defaultdict(list)
    for e in g.edges.values():
        if e.type in (EdgeType.CALLS, EdgeType.REFERENCES):
            out[e.src].append(e)
    syms = g.symbols
    new = {}

    def add(src, dst, conf, role, line):
        key = (src, dst, EdgeType.TESTS)
        old = new.get(key)
        if old is None or conf > old.confidence:
            new[key] = Edge(src, dst, EdgeType.TESTS, conf, Tier.DERIVED, role, 1, [line], [role])

    for sid, s in syms.items():
        if not s.flags & Flag.IS_TEST:
            continue
        for e in out.get(sid, []):
            t = syms[e.dst]
            if not t.flags & Flag.IN_TEST_FILE:
                add(sid, e.dst, TESTS_DIRECT * e.confidence, "direct", e.lines[0] if e.lines else s.start_line)
            elif not t.flags & Flag.IS_TEST and t.kind != Kind.MODULE:
                for e2 in out.get(e.dst, []):
                    t2 = syms[e2.dst]
                    if not t2.flags & Flag.IN_TEST_FILE:
                        add(sid, e2.dst, TESTS_VIA_HELPER * e.confidence * e2.confidence, "via_helper",
                            e.lines[0] if e.lines else s.start_line)
    by_basename = defaultdict(list)
    for sid, s in syms.items():
        if s.kind == Kind.MODULE and not s.flags & Flag.IN_TEST_FILE:
            by_basename[posixpath.basename(s.path)].append(sid)
    for sid, s in syms.items():
        if s.kind == Kind.MODULE and s.flags & Flag.IN_TEST_FILE:
            base = posixpath.basename(s.path)
            if base.startswith("test_"):
                cands = by_basename.get(base[5:], [])
                if len(cands) == 1:
                    add(sid, cands[0], TESTS_BY_NAME, "naming", 1)
    for k, e in new.items():
        if e.src != e.dst:
            g.edges[k] = e
