"""Review-context retrieval pipeline v2 (design §5).

    RepoRetriever(graph, facts=...).build_context(diff_text, head_content, pr_text, cochange=...) -> ContextResult

Stages: parse diff -> link seeds -> candidate channels -> fuse -> pack -> trace.
Channels: lexical (BM25F, System A), structural (typed graph expansion, System E; with an
overlay for structurally changed files), cochange (history-derived file coupling).
No network calls, no LLM calls.
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
from backend.retrieval.structural import expand, rrf


@dataclass
class RetrievalConfig:
    budget: int = 12000
    seed_cap_frac: float = 0.4
    channels: tuple = ("lexical",)  # any of: lexical, structural, cochange
    lexical_k: int = 50  # candidates per seed from BM25F
    structural_limit: int = 80
    use_plan: bool = True  # structural: weight relations by the seed's change-type plan
    overlay: bool = True  # structural: re-resolve structurally changed files at HEAD (needs base facts)
    structural_same_file: bool = True  # structural: offer symbols of the changed files (unchanged callees etc.)
    cochange_files: int = 5  # co-change: coupled files per changed file
    cochange_min_conf: float = 0.1
    cochange_limit: int = 30
    rrf_k: int = 60
    channel_weights: dict | None = None  # RRF weights per channel
    max_candidates: int = 40  # after merging seeds / fusing channels
    min_rel_score: float = 0.15  # lexical: drop candidates scoring below this fraction of the best
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
    """Holds the immutable per-snapshot indexes; build once per (repo, commit), query per review.

    ``facts`` (the base FileFacts) enable the overlay: when a change is structural at the AST
    level, the changed files are re-resolved at their HEAD version against the rest of the
    repository, so new calls and new symbols are visible to the structural channel."""

    def __init__(self, graph: Graph, config: RetrievalConfig | None = None, facts: list | None = None):
        t0 = time.perf_counter()
        self.config = config or RetrievalConfig()
        self.repo_id = graph.repo_id
        self.snap = GraphSnapshot(graph)
        self.facts = {f.path: f for f in facts} if facts else None
        self._overlay_cache: dict = {}
        docs = [(i, _doc_fields(s)) for i, s in enumerate(self.snap.symbols) if s.kind != Kind.MODULE]
        self.lexical = BM25F(docs, self.config.bm25f_weights)
        self.build_seconds = time.perf_counter() - t0

    # ------------------------------------------------------------------ channels
    def _lexical_candidates(self, seeds, exclude: set[int], pr_text: str, cfg) -> list[tuple[int, float, str]]:
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
        return merged

    def _cochange_candidates(self, change, exclude: set[int], pr_text: str, cochange, cfg) -> list[tuple[int, float, str]]:
        """Symbols of files that historically change together with the changed files: files by
        coupling strength, symbols within a file by lexical similarity to the change (design §5.3)."""
        files: dict[str, float] = {}
        for p in sorted(change.changed_paths):
            for other, conf in cochange(p)[: cfg.cochange_files]:
                if other not in change.changed_paths and conf >= cfg.cochange_min_conf:
                    files[other] = max(files.get(other, 0.0), conf)
        if not files:
            return []
        q: dict[str, float] = {}
        for sd in change.seeds:
            for t, w in query_from_lines(sd.query_lines, pr_text, cfg.pr_text_weight).items():
                q[t] = max(q.get(t, 0.0), w)
        lex = dict(self.lexical.search(q, 5000, exclude)) if q else {}
        per_file = []
        for path, conf in sorted(files.items(), key=lambda kv: (-kv[1], kv[0])):
            idx = [i for i in self.snap.symbols_in(path) if i not in exclude and self.snap.symbols[i].kind != Kind.MODULE]
            idx.sort(key=lambda i: (-lex.get(i, 0.0), i))
            per_file.append([(i, conf * (1.0 + lex.get(i, 0.0)), "cochange") for i in idx])
        out = []
        for r in range(max((len(x) for x in per_file), default=0)):
            for lst in per_file:
                if r < len(lst):
                    out.append(lst[r])
        return out[: cfg.cochange_limit]

    def _overlay_out(self, change, head_content) -> dict | None:
        """Outgoing dependencies of each seed resolved on the HEAD version of the changed files."""
        if not change.structural_change or self.facts is None:
            return None
        from backend.kg.build import build_graph
        from backend.kg.extract_python import extract
        from backend.kg.model import EdgeType, symbol_id

        heads = {p: head_content(p) for p in sorted(change.changed_paths) if p.endswith(".py")}
        key = tuple((p, hash(t)) for p, t in heads.items())
        if key in self._overlay_cache:  # several channel configurations on the same change share one overlay
            og = self._overlay_cache[key]
        else:
            facts = dict(self.facts)
            for path, text in heads.items():
                if text is None:
                    facts.pop(path, None)
                else:
                    facts[path] = extract(path, text)
            og = build_graph(self.repo_id, list(facts.values()))
            self._overlay_cache = {key: og}
        wanted = {symbol_id(self.repo_id, sd.path, sd.qualname): sd.key for sd in change.seeds}
        out: dict[str, list] = {}
        for e in og.edges.values():
            if e.type in (EdgeType.CALLS, EdgeType.REFERENCES) and e.src in wanted:
                j = self.snap.index.get(e.dst)
                if j is not None:
                    out.setdefault(wanted[e.src], []).append((j, e.type, e.confidence, list(e.lines)))
        return out

    def _candidates(self, change, exclude: set[int], pr_text: str, cfg, head_content, cochange) -> tuple[list, dict]:
        per_channel = {}
        if "lexical" in cfg.channels:
            per_channel["lexical"] = self._lexical_candidates(change.seeds, exclude, pr_text, cfg)
        if "structural" in cfg.channels:
            overlay = self._overlay_out(change, head_content) if cfg.overlay else None
            st = expand(self.snap, change.seeds, cfg.structural_limit, use_plan=cfg.use_plan, overlay_out=overlay)
            if not cfg.structural_same_file:
                st = [c for c in st if self.snap.symbols[c[0]].path not in change.changed_paths]
            per_channel["structural"] = st
        if "cochange" in cfg.channels and cochange is not None:
            per_channel["cochange"] = self._cochange_candidates(change, exclude, pr_text, cochange, cfg)
        if len(per_channel) == 1:
            fused = next(iter(per_channel.values()))
        else:
            fused = rrf(per_channel, cfg.rrf_k, cfg.channel_weights)
        return fused[: cfg.max_candidates], {k: len(v) for k, v in per_channel.items()}

    # ------------------------------------------------------------------ entry point
    def build_context(self, diff_text: str, head_content: Callable[[str], str | None], pr_text: str = "",
                      config: RetrievalConfig | None = None, cochange=None) -> ContextResult:
        """``cochange``: optional callable path -> [(other_path, confidence)] (history-derived coupling)."""
        cfg = config or self.config
        timings = {}
        t0 = time.perf_counter()
        files = parse_unified_diff(diff_text)
        change = link(self.snap, files, head_content)
        timings["link_ms"] = (time.perf_counter() - t0) * 1000
        exclude = {i for p in change.changed_paths for i in self.snap.symbols_in(p)}
        exclude |= {sd.index for sd in change.seeds if sd.index is not None}
        t1 = time.perf_counter()
        candidates, channel_sizes = self._candidates(change, exclude, pr_text, cfg, head_content, cochange)
        timings["candidates_ms"] = (time.perf_counter() - t1) * 1000
        t2 = time.perf_counter()
        packed = pack(self.snap, change.seeds, candidates, cfg.budget, cfg.seed_cap_frac)
        timings["pack_ms"] = (time.perf_counter() - t2) * 1000
        timings["total_ms"] = (time.perf_counter() - t0) * 1000
        trace = {
            "config": asdict(cfg),
            "seeds": [{"key": s.key, "change": s.change, "lines": sorted(s.lines)[:50], "in_base": s.index is not None,
                       "plan": PLAN.get(s.change, [])} for s in change.seeds],
            "structural_change": change.structural_change,
            "channel_sizes": channel_sizes,
            "skipped_files": change.skipped_files,
            "candidates": [{"key": f"{self.snap.symbols[i].path}::{self.snap.symbols[i].qualname}", "score": round(sc, 4),
                            "channel": ch} for i, sc, ch in candidates],
            "packed": [{"key": it.key, "rendering": it.rendering, "chars": it.chars, "label": it.label} for it in packed.items],
            "dropped": packed.dropped,
            "chars": packed.chars,
            "timings_ms": {k: round(v, 2) for k, v in timings.items()},
        }
        return ContextResult(packed.text, packed.items, trace)
