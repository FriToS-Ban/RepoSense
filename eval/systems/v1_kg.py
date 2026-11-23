"""Structural statistics and edge quality of the v1 knowledge graph vs the reference resolver.

Reproduces docs/retrieval/01-phase1-audit.md §4.2.  The bare-Name call-site audit is
the reliable precision estimate: for ``foo(...)`` calls, scope + import resolution in
the reference resolver is near-exact, so a v1 edge created for that name can be
classified as correct, wrong target, or spurious (local/builtin/external).
"""
from __future__ import annotations

import re
from collections import Counter

from eval.reference import RefGraph
from eval.systems.v1 import base_name

CODE_EXT = (".py", ".js", ".ts", ".jsx", ".tsx")


def approx_tokens(text: str) -> int:
    """Lexical token count; a lower bound on BPE tokens for code."""
    return len(re.findall(r"\w+|[^\w\s]", text))


def _pr(cur: set, ref: set) -> dict:
    tp = len(cur & ref)
    return {
        "cur": len(cur),
        "ref": len(ref),
        "precision": round(tp / len(cur), 3) if cur else None,
        "recall": round(tp / len(ref), 3) if ref else None,
    }


def kg_stats(db, repo_id: str, files: list[dict], rg: RefGraph) -> dict:
    from backend.models.models import CodeEdge, CodeNode, EdgeType, NodeType

    nodes = db.query(CodeNode).filter(CodeNode.repo_id == repo_id).all()
    by_id = {n.id: n for n in nodes}
    edges = (
        db.query(CodeEdge).join(CodeNode, CodeEdge.source_id == CodeNode.id).filter(CodeNode.repo_id == repo_id).all()
    )
    st: dict = {"files": sum(1 for f in files if f["path"].endswith(CODE_EXT))}
    st["loc"] = sum(f["content"].count("\n") for f in files if f["path"].endswith(".py"))
    st["nodes"] = dict(sorted(Counter(n.node_type.value for n in nodes).items()))
    st["chunk_nodes"] = sum(1 for n in nodes if "__chunk" in (n.name or ""))
    st["embedded"] = sum(1 for n in nodes if n.pinecone_id)
    st["not_embedded"] = len(nodes) - st["embedded"]
    groups = Counter((n.file_path, n.name) for n in nodes if n.node_type != NodeType.file and "__chunk" not in n.name)
    st["nodes_in_name_collisions"] = sum(c for c in groups.values() if c > 1)
    st["ref_symbols"] = dict(sorted(Counter(s["kind"] for s in rg.syms.values()).items()))
    emb_chars = sum(len(f"{n.file_path}\n{n.name}\n{n.content or ''}"[:8000]) for n in nodes if n.pinecone_id)
    src_chars = sum(len(f["content"]) for f in files if f["path"].endswith(CODE_EXT))
    st["embedded_chars_over_source_chars"] = round(emb_chars / max(src_chars, 1), 2)
    toks = [approx_tokens(f"{n.file_path}\n{n.name}\n{n.content or ''}"[:8000]) for n in nodes if n.pinecone_id]
    st["frac_embedded_texts_over_512_lexical_tokens"] = round(sum(t > 512 for t in toks) / max(len(toks), 1), 3)
    outdeg = Counter(e.source_id for e in edges)
    st["edges"] = dict(sorted(Counter(e.edge_type.value for e in edges).items()))
    for t in ("file", "function", "class"):
        ns = [n for n in nodes if n.node_type.value == t]
        st[f"frac_{t}_nodes_with_outgoing_edges"] = round(sum(1 for n in ns if outdeg[n.id]) / max(len(ns), 1), 3)

    cur_imp = {(by_id[e.source_id].file_path, by_id[e.target_id].file_path) for e in edges if e.edge_type == EdgeType.imports}
    calls_raw = [e for e in edges if e.edge_type == EdgeType.calls]
    cur_call = {(by_id[e.source_id].file_path, by_id[e.target_id].file_path, base_name(by_id[e.target_id].name)) for e in calls_raw}
    cur_inh = {
        ((by_id[e.source_id].file_path, base_name(by_id[e.source_id].name)), (by_id[e.target_id].file_path, base_name(by_id[e.target_id].name)))
        for e in edges
        if e.edge_type == EdgeType.inherits
    }
    ref_call_x, ref_call_same = rg.call_pairs()
    st["edge_quality"] = {
        "imports(file->file)": _pr(cur_imp, rg.import_file_pairs()),
        "calls(file->fn), set-level vs resolved cross-file calls": _pr(cur_call, ref_call_x),
        "inherits(class->class)": _pr(cur_inh, rg.inherit_pairs()),
    }
    st["calls_raw_edges"] = len(calls_raw)
    st["calls_unique_pairs"] = len(cur_call)
    st["ref_same_file_call_pairs_never_captured"] = len(ref_call_same)

    cur_target = {}
    for e in calls_raw:
        s, t = by_id[e.source_id], by_id[e.target_id]
        cur_target[(s.file_path, base_name(t.name))] = t.file_path
    c: Counter = Counter()
    for sid in sorted(rg.name_calls):
        f = sid[0]
        for name, tgt in rg.name_calls[sid]:
            tsym = rg.syms.get(tgt) if tgt else None
            cross_true = tsym is not None and tsym["kind"] in ("function", "method") and tsym["file"] != f
            if (f, name) in cur_target:
                if cross_true and tsym["file"] == cur_target[(f, name)] and tsym["name"] == name:
                    c["edge_correct"] += 1
                elif tsym is not None:
                    c["edge_wrong_target"] += 1
                else:
                    c["edge_spurious(local/builtin/external)"] += 1
            if cross_true:
                c["true_cross_file_name_calls"] += 1
                if cur_target.get((f, name)) == tsym["file"]:
                    c["true_cross_file_name_calls_captured"] += 1
    tot = c["edge_correct"] + c["edge_wrong_target"] + c["edge_spurious(local/builtin/external)"]
    st["name_callsite_audit"] = dict(sorted(c.items()))
    st["name_callsite_precision"] = round(c["edge_correct"] / tot, 3) if tot else None
    st["name_callsite_recall"] = (
        round(c["true_cross_file_name_calls_captured"] / c["true_cross_file_name_calls"], 3) if c["true_cross_file_name_calls"] else None
    )
    fn_names = Counter(base_name(n.name) for n in nodes if n.node_type == NodeType.function and "__chunk" not in n.name)
    st["frac_call_edges_with_ambiguous_target_name"] = round(
        sum(1 for e in calls_raw if fn_names[base_name(by_id[e.target_id].name)] > 1) / max(len(calls_raw), 1), 3
    )
    return st
