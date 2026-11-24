"""Review context from retrieval v2, used when RETRIEVAL_ENGINE=v2 (design §8).

The graph of the repository's current snapshot is read from the kg_* tables once and kept
in a small LRU of RepoRetriever objects keyed by (repo, snapshot commit), so a review costs
the query only (tens of milliseconds; design §5.7).  Every review logs its trace summary.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Callable

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
CACHE_SIZE = 4
_cache: "OrderedDict[tuple[str, str], object]" = OrderedDict()
_cache_lock = threading.Lock()


def production_config():
    """The configuration v2 serves.  System A (BM25F + relation-aware packing) is the best measured
    system on T2 so far (docs/retrieval/results/M4.md–M6.md); M9/M11 revisit the choice."""
    from backend.retrieval.pipeline import RetrievalConfig

    return RetrievalConfig(channels=("lexical",))


def _retriever(db: Session, repo_id: str, sha: str, need_facts: bool):
    from sqlalchemy import select

    from backend.kg.model import FileFacts
    from backend.kg.store import load_graph
    from backend.models.models import KGFile
    from backend.retrieval.pipeline import RepoRetriever

    key = (repo_id, sha)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    graph = load_graph(db, repo_id)
    facts = None
    if need_facts:
        facts = [FileFacts.from_json(r.facts_json) for r in db.execute(select(KGFile).where(KGFile.repo_id == repo_id)).scalars()]
    retr = RepoRetriever(graph, production_config(), facts=facts)
    with _cache_lock:
        _cache[key] = retr
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return retr


def get_context_v2(db: Session, repo_id: str, diff: str, head_content: Callable[[str], str | None], pr_text: str = "",
                   config=None) -> str | None:
    """The v2 context, or None when the repository has no knowledge-graph snapshot yet (the caller
    then falls back to v1; review.v2_inputs has already requested the first index)."""
    from backend.services.kg_indexer import current_snapshot

    sha = current_snapshot(db, repo_id)
    if sha is None:
        logger.info("kg v2: repository %s has no snapshot yet; falling back to v1", repo_id)
        return None
    cfg = config or production_config()
    retr = _retriever(db, repo_id, sha, need_facts=any(c in cfg.channels for c in ("structural", "ppr")))
    res = retr.build_context(diff, head_content, pr_text, config=cfg)
    t = res.trace
    logger.info("kg v2 context: repo=%s snapshot=%s seeds=%d items=%d chars=%d ms=%.1f", repo_id, sha[:10],
                len(t["seeds"]), len(t["packed"]), t["chars"], t["timings_ms"]["total_ms"])
    return res.text
