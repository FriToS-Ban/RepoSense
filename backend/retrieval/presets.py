"""Named retrieval configurations chosen by the milestones' experiments.

TUNED_LEXICAL: the lexical-channel settings selected on T2-train by eval/tune_lexical.py (M6;
configuration "rand20", +.029 R@budget on T2-train over the M2 defaults) and validated on
T2-test (docs/retrieval/results/M6.md).  RetrievalConfig's own defaults stay at the M2 values
so that earlier systems remain reproducible.

production_config(): the system v2 serves after the cutover (M11, docs/retrieval/results/M11.md):
learned fusion over the lexical, structural and PPR channels (the M9 model trained on T2-train
without co-change features, which production has no history index for), with callees
referenced on changed lines pinned first (T1/T3 need them, history cannot reward them), and
without the overlay graph (no measurable effect on T2, the largest latency cost).
"""
from functools import lru_cache
from pathlib import Path

TUNED_LEXICAL: dict = {
    "bm25f_weights": {"name": 1.0, "qual": 1.5, "sig": 3.0, "doc": 0.5, "body": 1.0},
    "bm25_k1": 0.8,
    "bm25_b": 0.75,
    "pr_text_weight": 2.0,
    "context_weight": 0.5,
    "lexical_k": 50,
    "min_rel_score": 0.15,
    "max_candidates": 60,
    "residual_weight": 2.0,
}

PRODUCTION_MODEL = Path(__file__).with_name("models") / "ltr_f_nocc.json"  # = eval/results/m9-ltr/model_F_nocc.json


@lru_cache(maxsize=1)
def production_model():
    from backend.retrieval.ltr import LinearRanker

    return LinearRanker.load(PRODUCTION_MODEL)


def production_config():
    from backend.retrieval.pipeline import RetrievalConfig

    m = production_model()
    return RetrievalConfig(channels=tuple(m.meta["generators"]), reranker=m, rerank_pin=("callee_on_changed_line",),
                           overlay=False, **TUNED_LEXICAL)
