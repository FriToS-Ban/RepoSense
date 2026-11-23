"""Review-context retrieval pipeline v2 (design §5).

    RepoRetriever(graph).build_context(diff_text, head_content, pr_text) -> ContextResult

Stages: parse diff -> link seeds -> candidate channels -> rank -> pack -> trace.
M2 implements the lexical channel (BM25F) = System A; the structural, co-change and
dense channels plug in as further channels (M3+).  No network calls, no LLM calls.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from backend.kg.model import Graph, Kind
from backend.kg.snapshot import GraphSnapshot
from backend.retrieval.diff import parse_unified_diff
from backend.retrieval.lexical import BM25F, query_from_lines
from backend.retrieval.packing import Item, pack
from backend.retrieval.seeds import PLAN, link


@dataclass
class RetrievalConfig:
    budget: int = 12000
    seed_cap_frac: float = 0.4
    lexical_k: int = 50  # candidates per seed from BM25F
    max_candidates: int = 40  # after merging seeds
    min_rel_score: float = 0.15  # drop candidates scoring below this fraction of the best
    bm25f_weights: dict | None = None
    pr_text_weight: float = 1.0


@dataclass
class ContextResult:
    text: str
    items: list[Item]
    trace: dict = field(default_factory=dict)


def _doc_fields(s) -> dict[str, str]:
    return {"name": s.name, "qual": f"{s.qualname} {s.path}", "sig": s.signature, "doc": s.docstring, "body": s.body}


class RepoRetriever:
    """Holds the immutable per-snapshot indexes; build once per (repo, commit), query per review."""

    def __init__(self, graph: Graph, config: RetrievalConfig | None = None):
        t0 = time.perf_counter()
        self.config = config or RetrievalConfig()
        self.snap = GraphSnapshot(graph)
        docs = [(i, _doc_fields(s)) for i, s in enumerate(self.snap.symbols) if s.kind != Kind.MODULE]
        self.lexical = BM25F(docs, self.config.bm25f_weights)
        self.build_seconds = time.perf_counter() - t0

    def _lexical_candidates(self, seeds, exclude: set[int], pr_text: str) -> list[tuple[int, float, str]]:
        cfg = self.config
        per_seed = []
        for sd in seeds:
            q = query_from_lines(sd.query_lines, pr_text, cfg.pr_text_weight)
            per_seed.append(self.lexical.search(q, cfg.lexical_k, exclude) if q else [])
        if not seeds and pr_text:
            per_seed.append(self.lexical.search(query_from_lines([], pr_text), cfg.lexical_k, exclude))
        # round-robin across seeds so that one large hunk cannot crowd out the others
        merged, seen = [], set()
        for r in range(max((len(x) for x in per_seed), default=0)):
            for lst in per_seed:
                if r < len(lst) and lst[r][0] not in seen:
                    seen.add(lst[r][0])
                    merged.append((lst[r][0], lst[r][1], "lexical"))
        if merged:
            top = max(s for _, s, _ in merged)
            merged = [c for c in merged if c[1] >= cfg.min_rel_score * top]
        return merged[: cfg.max_candidates]

    def build_context(self, diff_text: str, head_content: Callable[[str], str | None], pr_text: str = "") -> ContextResult:
        timings = {}
        t0 = time.perf_counter()
        files = parse_unified_diff(diff_text)
        change = link(self.snap, files, head_content)
        timings["link_ms"] = (time.perf_counter() - t0) * 1000
        exclude = {i for p in change.changed_paths for i in self.snap.symbols_in(p)}
        exclude |= {sd.index for sd in change.seeds if sd.index is not None}
        t1 = time.perf_counter()
        candidates = self._lexical_candidates(change.seeds, exclude, pr_text)
        timings["candidates_ms"] = (time.perf_counter() - t1) * 1000
        t2 = time.perf_counter()
        packed = pack(self.snap, change.seeds, candidates, self.config.budget, self.config.seed_cap_frac)
        timings["pack_ms"] = (time.perf_counter() - t2) * 1000
        timings["total_ms"] = (time.perf_counter() - t0) * 1000
        trace = {
            "config": asdict(self.config),
            "seeds": [{"key": s.key, "change": s.change, "lines": sorted(s.lines)[:50], "in_base": s.index is not None,
                       "plan": PLAN.get(s.change, [])} for s in change.seeds],
            "structural_change": change.structural_change,
            "skipped_files": change.skipped_files,
            "candidates": [{"key": f"{self.snap.symbols[i].path}::{self.snap.symbols[i].qualname}", "score": round(sc, 4),
                            "channel": ch} for i, sc, ch in candidates],
            "packed": [{"key": it.key, "rendering": it.rendering, "chars": it.chars, "label": it.label} for it in packed.items],
            "dropped": packed.dropped,
            "chars": packed.chars,
            "timings_ms": {k: round(v, 2) for k, v in timings.items()},
        }
        return ContextResult(packed.text, packed.items, trace)
