"""Replay of the v1 retrieval stack (indexer + rag) exactly as audited, plus systems S0–S4.

The v1 source files are read from git at ``V1_COMMIT`` and executed as private
modules, so these baselines stay reproducible after backend/services/* change.

Systems (see docs/retrieval/01-phase1-audit.md §6.1):
  S0  v1 ``get_relevant_context`` as shipped at V1_COMMIT (raises internally -> "")
  S1  v1 pipeline with the one-line hotfix, first stage = oracle (ground-truth nodes)
  S2  v1 pipeline with the one-line hotfix, first stage = BM25 over the embedded node texts
  S3  raw BM25 ranking (top 50), no pipeline post-processing
  S4  v1 graph only: outgoing edges of the changed file's node(s)
"""
from __future__ import annotations

import re
import subprocess
import time
import types
from collections import Counter, defaultdict

from eval import offline
from eval.benchmarks.t1 import Query, make_diff
from eval.lexical import BM25

V1_COMMIT = "887d2fdb305275727d73c7bd84d4f63b67aa7e65"
HOTFIX_BUG = "        all_matched_nodes.extend(direct_nodes)\n\n        all_matched_nodes: List[CodeNode] = []\n"
HOTFIX_FIX = "        all_matched_nodes: List[CodeNode] = []\n        all_matched_nodes.extend(direct_nodes)\n"
SYSTEMS = ("S0_as_shipped", "S1_fixed_pipeline+oracle_stage1", "S2_fixed_pipeline+bm25_stage1", "S3_bm25_raw", "S4_current_graph_only")


def git_show(commit: str, path: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(offline.REPO_ROOT), "show", f"{commit}:{path}"], capture_output=True, check=True
    ).stdout.decode("utf-8")
    return out.replace("\r\n", "\n")


def apply_hotfix(rag_source: str) -> str:
    if HOTFIX_BUG not in rag_source:
        raise RuntimeError("v1 hotfix pattern not found")
    return rag_source.replace(HOTFIX_BUG, HOTFIX_FIX)


def _module(name: str, source: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__file__ = f"<{name}>"
    exec(compile(source, f"{name}.py", "exec"), mod.__dict__)
    return mod


class V1:
    """Requires ``offline.bootstrap`` to have run."""

    def __init__(self, commit: str = V1_COMMIT):
        rag_src = git_show(commit, "backend/services/rag.py")
        self.rag_shipped = _module("v1_rag_shipped", rag_src)
        self.rag_fixed = _module("v1_rag_fixed", apply_hotfix(rag_src))
        self.indexer = _module("v1_indexer", git_show(commit, "backend/services/indexer.py"))
        self.indexer.store_node_in_pinecone = self.rag_fixed.store_node_in_pinecone


# ------------------------------------------------------------------ node keys
def base_name(name: str | None) -> str:
    return (name or "").split("__chunk")[0]


def node_key(node) -> str:
    """Symbol nodes -> "path::name"; file nodes -> "path" (never relevant in symbol views)."""
    if node.node_type.value == "file":
        return node.file_path
    return f"{node.file_path}::{base_name(node.name)}"


def key_path(key: str) -> str:
    return key.split("::")[0]


# ------------------------------------------------------------------ first-stage control for the fake Pinecone index
class StageOne:
    def __init__(self):
        self.mode = "bm25"
        self.oracle: list[str] = []
        self.returned: list[str] = []
        self.bm25: dict[str, BM25] = {}

    def __call__(self, text, top_k, flt, index):
        if self.mode == "oracle":
            hits = [(nid, 1.0) for nid in self.oracle][:top_k]
        else:
            hits = self.bm25[flt.get("repo_id")].search(text, top_k)
        self.returned.extend(n for n, _ in hits)
        return hits


HEADER_RE = re.compile(r"^=== (.+?) \((file|function|class): (.+?)\) ===$", re.M)


def parse_context(ctx: str) -> list[tuple[str, int, str]]:
    """[(key, chars, path)] in context order, from v1's '=== path (type: name) ===' headers."""
    items = []
    ms = list(HEADER_RE.finditer(ctx))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(ctx)
        path, kind, name = m.group(1), m.group(2), m.group(3)
        items.append((path if kind == "file" else f"{path}::{name}", end - m.start(), path))
    return items


class V1Bench:
    """Indexes corpora with the v1 indexer and answers T1 queries with systems S0–S4."""

    def __init__(self, v1: V1, db, sql_counter: Counter):
        from backend.models.models import CodeNode, NodeType

        self.CodeNode, self.NodeType = CodeNode, NodeType
        self.v1, self.db, self.sql = v1, db, sql_counter
        self.stage1 = StageOne()
        offline.STORE.query_hook = self.stage1
        self.meta: dict[str, tuple] = {}  # node_id -> (key, path, type, embedded, name)
        self.key2nodes: dict[str, dict[str, list]] = {}  # repo -> key -> [(is_chunk, node_id)]

    # -------------------------------------------------------------- indexing
    def index(self, name: str, files: list[dict], repo_id: str) -> dict:
        from backend.models.models import Repository

        self.db.add(Repository(id=repo_id, repo_name=name, repo_full_name=f"bench/{name}", is_indexed=True, crawl_permission=True))
        self.db.commit()
        before = Counter(offline.COUNTERS)
        sql0 = self.sql["stmts"]
        t0 = time.perf_counter()
        self.v1.indexer.index_files(repo_id, files, self.db, clear_first=True)
        seconds = time.perf_counter() - t0
        calls = {k: offline.COUNTERS[k] - before.get(k, 0) for k in offline.COUNTERS if offline.COUNTERS[k] - before.get(k, 0)}
        calls["sql.stmts"] = self.sql["stmts"] - sql0
        nodes = self.db.query(self.CodeNode).filter(self.CodeNode.repo_id == repo_id).all()
        k2n = defaultdict(list)
        for n in nodes:
            self.meta[n.id] = (node_key(n), n.file_path, n.node_type.value, bool(n.pinecone_id), n.name)
            if n.pinecone_id and n.node_type.value != "file":
                k2n[node_key(n)].append(("__chunk" in n.name, n.id))
        self.key2nodes[repo_id] = k2n
        self.stage1.bm25[repo_id] = BM25(offline.STORE.docs_for_repo(repo_id))
        return {"seconds_local_cpu": round(seconds, 2), "calls": calls}

    # -------------------------------------------------------------- systems
    def _pipeline(self, fn, repo_id, diff, path):
        """One review = one fresh DB session, as in review.process_pr_review (makes SQL counts history-independent)."""
        from backend.core.database import SessionLocal

        self.stage1.returned = []
        sql0, e0, q0 = self.sql["stmts"], offline.COUNTERS["embed.calls"], offline.COUNTERS["pinecone.query"]
        db = SessionLocal()
        t0 = time.perf_counter()
        try:
            ctx = fn(repo_id, diff, db, changed_file_paths=[path])
        finally:
            ms = (time.perf_counter() - t0) * 1000
            db.close()
        cost = {"sql": self.sql["stmts"] - sql0, "embed": offline.COUNTERS["embed.calls"] - e0,
                "pinecone": offline.COUNTERS["pinecone.query"] - q0}
        return parse_context(ctx), ms, list(self.stage1.returned), cost

    def run_query(self, q: Query, repo_id: str, lines: list[str]) -> dict:
        diff = make_diff(q.path, lines, q.ws, q.we, q.category)
        k2n = self.key2nodes[repo_id]
        oracle = []
        for k in q.main_gt():
            cands = sorted(k2n.get(k, []))
            if cands:
                oracle.append(cands[0][1])
        out = {"ranked": {}, "latency_ms": {}, "context": {}, "returned": {}, "cost": {}}
        rf, rs = self.v1.rag_fixed, self.v1.rag_shipped
        self.stage1.mode, self.stage1.oracle = "oracle", oracle
        for sys_name, fn, mode in (
            ("S1_fixed_pipeline+oracle_stage1", rf.get_relevant_context, "oracle"),
            ("S2_fixed_pipeline+bm25_stage1", rf.get_relevant_context, "bm25"),
            ("S0_as_shipped", rs.get_relevant_context, "bm25"),
        ):
            self.stage1.mode = mode
            items, ms, returned, cost = self._pipeline(fn, repo_id, diff, q.path)
            out["ranked"][sys_name] = [k for k, _, _ in items]
            out["context"][sys_name] = items
            out["returned"][sys_name] = returned
            out["cost"][sys_name] = cost
            out["latency_ms"][sys_name] = ms
        hunk = rf.extract_diff_chunks(diff)[-1][1]
        t0 = time.perf_counter()
        hits = self.stage1.bm25[repo_id].search(hunk, 50)
        out["latency_ms"]["S3_bm25_raw"] = (time.perf_counter() - t0) * 1000
        out["ranked"]["S3_bm25_raw"] = [self.meta[h][0] for h, _ in hits]
        t0 = time.perf_counter()
        CN = self.CodeNode
        fnodes = self.db.query(CN).filter(CN.repo_id == repo_id, CN.file_path == q.path, CN.node_type == self.NodeType.file).all()
        out["ranked"]["S4_current_graph_only"] = [self.meta[e.target_id][0] for f in fnodes for e in f.outgoing_edges]
        out["latency_ms"]["S4_current_graph_only"] = (time.perf_counter() - t0) * 1000
        return out
